"""真实安装并验证 A1/A2/A3（慢，下载 2-3GB）。
运行：VIBECAD_RUN_INTEGRATION=1 uv run pytest -m slow tests/test_runtime_integration.py -v -s
"""

import base64
import contextlib
import hashlib
import json
import os
import secrets
import shutil
import stat
import subprocess
import sys
import threading
import time
from collections.abc import Mapping
from pathlib import Path

import pytest

from vibecad.runtime import paths, spec, status
from vibecad.runtime.installer import RuntimeInstaller

pytestmark = pytest.mark.slow
_RUN = os.environ.get("VIBECAD_RUN_INTEGRATION") == "1"
_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SRC = os.path.join(_REPO, "src")
_FRESH_MIGRATION_CONFIRMATION = "install-current-managed-preserve-external"


@pytest.fixture
def _fresh_migration_maintenance_guard():
    """Hold the stable maintenance authority across preflight, snapshots, and install."""

    with status.runtime_maintenance_lock():
        yield


def _thaw_json(value: object) -> object:
    """Copy the frozen checkout contract into a plain JSON projection."""

    if value is None or type(value) in {str, int, float, bool}:
        return value
    if type(value) in {tuple, list}:
        return [_thaw_json(item) for item in value]
    if isinstance(value, Mapping):
        return {key: _thaw_json(value[key]) for key in value}
    raise TypeError(f"unsupported public metadata value: {type(value)!r}")


def _expected_tool_projection() -> list[dict[str, object]]:
    """Independent checkout-side projection for the installed MCPB response."""

    from vibecad.application.public_surface import public_tool_specs

    return [
        {
            "name": item.name,
            "description": item.description,
            "inputSchema": _thaw_json(item.input_schema),
            "annotations": {
                "readOnlyHint": item.annotations.read_only,
                "destructiveHint": item.annotations.destructive,
                "idempotentHint": item.annotations.idempotent,
                "openWorldHint": item.annotations.open_world,
            },
        }
        for item in public_tool_specs()
    ]


def _sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _assert_fresh_unpacked_package(unpacked: Path) -> None:
    """Bind acceptance to a fresh, byte-exact copy of the frozen checkout."""

    repository = Path(_REPO).resolve()
    assert unpacked != repository
    assert not unpacked.is_symlink()
    fixed_files = {
        "LICENSE",
        "PRIVACY.md",
        "README.md",
        "icon.png",
        "manifest.json",
        "mcpb_entry.py",
        "pyproject.toml",
        "uv.lock",
    }
    assert {path.name for path in unpacked.iterdir()} == {*fixed_files, "skills", "src"}
    source_root = unpacked / "src"
    package_root = source_root / "vibecad"
    assert source_root.is_dir() and not source_root.is_symlink()
    assert package_root.is_dir() and not package_root.is_symlink()
    for name in fixed_files:
        packaged = unpacked / name
        checked_in = repository / name
        assert packaged.is_file() and not packaged.is_symlink()
        assert packaged.read_bytes() == checked_in.read_bytes()
        assert _sha256_path(packaged) == _sha256_path(checked_in)

    checked_in_source = {
        path.relative_to(repository / "src" / "vibecad").as_posix(): _sha256_path(path)
        for path in (repository / "src" / "vibecad").rglob("*.py")
        if path.is_file() and not path.is_symlink()
    }
    packaged_source: dict[str, str] = {}
    for path in package_root.rglob("*"):
        assert not path.is_symlink()
        if path.is_dir():
            continue
        assert path.is_file() and path.suffix == ".py"
        packaged_source[path.relative_to(package_root).as_posix()] = _sha256_path(path)
    assert packaged_source == checked_in_source

    checked_in_skill_root = repository / "skills" / "vibecad-agent"
    packaged_skill_root = unpacked / "skills" / "vibecad-agent"
    assert packaged_skill_root.is_dir() and not packaged_skill_root.is_symlink()
    checked_in_skill = {
        path.relative_to(checked_in_skill_root).as_posix(): _sha256_path(path)
        for path in checked_in_skill_root.rglob("*")
        if path.is_file() and not path.is_symlink()
    }
    packaged_skill: dict[str, str] = {}
    for path in packaged_skill_root.rglob("*"):
        assert not path.is_symlink()
        if path.is_dir():
            continue
        assert path.is_file()
        packaged_skill[path.relative_to(packaged_skill_root).as_posix()] = _sha256_path(path)
    assert packaged_skill == checked_in_skill
    assert not (unpacked / ".venv").exists(), "acceptance requires a newly unpacked package"


def _validated_mcpb_child_binding() -> tuple[Path, Path]:
    """Require one fresh private child home bound to the current managed engine."""

    raw = os.environ.get("VIBECAD_MCPB_EXTRA_ENV_JSON")
    if not raw:
        pytest.fail("set the exact child-only MCPB runtime environment")
    try:
        extra = json.loads(raw)
    except (TypeError, ValueError):
        pytest.fail("the child-only MCPB runtime environment is invalid")
    if type(extra) is not dict or set(extra) != {"VIBECAD_HOME", "VIBECAD_FREECAD_ENV"}:
        pytest.fail("the child-only MCPB runtime environment has unexpected fields")
    if any(type(value) is not str or not value for value in extra.values()):
        pytest.fail("the child-only MCPB runtime environment has invalid values")

    child_home = Path(extra["VIBECAD_HOME"])
    if not child_home.is_absolute() or str(child_home) != extra["VIBECAD_HOME"]:
        pytest.fail("the child VIBECAD_HOME must be one canonical absolute path")
    if os.path.lexists(child_home):
        pytest.fail("the child VIBECAD_HOME must not exist before packaged startup")
    parent = child_home.parent
    try:
        canonical_parent = parent.resolve(strict=True)
    except OSError:
        pytest.fail("the child VIBECAD_HOME parent is unavailable")
    if parent != canonical_parent or parent.is_symlink():
        pytest.fail("the child VIBECAD_HOME parent contains an alias")

    tmp_raw = os.environ.get("TMPDIR")
    if not tmp_raw:
        pytest.fail("TMPDIR is required for isolated packaged acceptance")
    try:
        private_tmp = Path(tmp_raw).expanduser().resolve(strict=True)
        relative_parent = parent.relative_to(private_tmp)
    except (OSError, ValueError):
        pytest.fail("the child VIBECAD_HOME must be below the user-private TMPDIR")
    directories = [private_tmp]
    current = private_tmp
    for part in relative_parent.parts:
        current /= part
        directories.append(current)
    current_uid = os.geteuid()
    for directory in directories:
        info = directory.lstat()
        if (
            directory.is_symlink()
            or not stat.S_ISDIR(info.st_mode)
            or info.st_uid != current_uid
            or stat.S_IMODE(info.st_mode) & 0o077
        ):
            pytest.fail("the child VIBECAD_HOME ancestry is not owner-private")

    managed = paths.env_prefix().resolve(strict=True)
    selected = Path(extra["VIBECAD_FREECAD_ENV"])
    try:
        selected_resolved = selected.resolve(strict=True)
    except OSError:
        pytest.fail("the child FreeCAD override is unavailable")
    if (
        not selected.is_absolute()
        or str(selected) != extra["VIBECAD_FREECAD_ENV"]
        or selected != selected_resolved
        or selected_resolved != managed
    ):
        pytest.fail("the child FreeCAD override must be the canonical managed prefix")
    return child_home, managed


def _tree_content_snapshot(root: Path) -> tuple[str, int]:
    """Hash a real tree's names, identities, metadata, links and regular-file bytes."""

    if not os.path.lexists(root):
        return ("missing", 0)
    root_info = root.lstat()
    if not root.is_dir() or root.is_symlink():
        raise AssertionError(f"snapshot root is not one real directory: {root}")
    digest = hashlib.sha256()
    count = 1

    def add_entry(path: Path, relative: str) -> None:
        nonlocal count
        info = path.lstat()
        fields = (
            relative,
            info.st_dev,
            info.st_ino,
            info.st_mode,
            info.st_nlink,
            info.st_size,
            info.st_mtime_ns,
        )
        digest.update(json.dumps(fields, separators=(",", ":")).encode("utf-8"))
        if path.is_symlink():
            digest.update(os.readlink(path).encode("utf-8", "surrogateescape"))
        elif path.is_file():
            digest.update(_sha256_path(path).encode("ascii"))
        count += 1

    digest.update(
        json.dumps(
            (".", root_info.st_dev, root_info.st_ino, root_info.st_mode, root_info.st_mtime_ns),
            separators=(",", ":"),
        ).encode("utf-8")
    )
    for current, directories, files in os.walk(root, topdown=True, followlinks=False):
        directories.sort()
        files.sort()
        current_path = Path(current)
        for name in [*directories, *files]:
            path = current_path / name
            add_entry(path, path.relative_to(root).as_posix())
    return digest.hexdigest(), count


def _single_file_snapshot(path: Path) -> tuple[object, ...]:
    if not os.path.lexists(path):
        return ("missing",)
    info = path.lstat()
    assert path.is_file() and not path.is_symlink()
    return (
        info.st_dev,
        info.st_ino,
        info.st_mode,
        info.st_nlink,
        info.st_size,
        info.st_mtime_ns,
        _sha256_path(path),
    )


@pytest.mark.skipif(not _RUN, reason="set VIBECAD_RUN_INTEGRATION=1")
def test_install_and_smoke(tmp_path, monkeypatch):
    monkeypatch.setenv("VIBECAD_HOME", str(tmp_path / "vchome"))
    monkeypatch.setenv("VIBECAD_PIP_SPEC", _REPO)  # 绝对路径装本地源（M8）
    phases = []
    RuntimeInstaller(on_progress=lambda s: phases.append(s.phase.value)).install()  # A2
    assert status.runtime_ready() is True
    runtime_python = paths.active_runtime_python()
    assert status.health_check(runtime_python) is True  # A1（subprocess 级）

    # A1 进程内 import 经 conda env python 子进程执行后回读（不在 pytest 进程 import）
    step = str(tmp_path / "vc_smoke.step")
    code = (
        status._PREP  # M-C：Windows DLL 兜底，否则 win CI 集成 import 必红
        + "import FreeCAD, Part; b=Part.makeBox(10,10,10);"
        f"assert abs(b.Volume-1000.0)<1e-6; b.exportStep({step!r}); print('OK')"
    )
    r = subprocess.run(
        [str(runtime_python), "-c", code],
        capture_output=True,
        text=True,
        env=status.freecad_process_environment(os.environ),
    )
    assert r.returncode == 0, r.stderr
    assert os.path.exists(step)
    assert phases[-1] == "ready"


@pytest.mark.skipif(
    not _RUN or os.environ.get("VIBECAD_RUN_FRESH_MIGRATION") != _FRESH_MIGRATION_CONFIRMATION,
    reason=(
        "set VIBECAD_RUN_INTEGRATION=1 and "
        f"VIBECAD_RUN_FRESH_MIGRATION={_FRESH_MIGRATION_CONFIRMATION}"
    ),
)
def test_real_pre_epoch_external_migrates_to_fresh_owned_current_runtime(
    _fresh_migration_maintenance_guard,
) -> None:
    """One explicitly authorized real migration; never runs in the default suite.

    The gate targets an existing pre-epoch legacy external runtime at its fixed
    location.  It must be incapable of satisfying the new receipt, remain byte-
    and inode-identical, and cause creation of a separate current managed runtime.
    A second explicit home variable prevents the opt-in token from accidentally
    targeting whatever platform default happens to be active.
    """

    from vibecad.runtime import spec

    selected_home = os.environ.get("VIBECAD_FRESH_MIGRATION_HOME")
    if not selected_home:
        pytest.fail("set VIBECAD_FRESH_MIGRATION_HOME to the reviewed migration home")
    reviewed_home = Path(selected_home).expanduser().resolve(strict=True)
    configured_home = paths.vibecad_home().expanduser().resolve(strict=True)
    if reviewed_home != configured_home:
        pytest.fail("VIBECAD_HOME and VIBECAD_FRESH_MIGRATION_HOME must identify one home")
    if paths.user_override_env() is not None:
        pytest.fail("fresh-owned migration must run without VIBECAD_FREECAD_ENV")
    pip_spec = os.environ.get("VIBECAD_PIP_SPEC")
    if not pip_spec or Path(pip_spec).expanduser().resolve(strict=True) != Path(_REPO).resolve():
        pytest.fail("VIBECAD_PIP_SPEC must be the reviewed repository checkout")

    current = paths.env_prefix()
    legacy = paths.legacy_env_prefix()
    legacy_receipt_path = legacy / ".vibecad_ready"
    binding_path = paths.external_runtime_receipt()
    if os.path.lexists(current):
        pytest.fail("fresh-owned migration requires the current managed prefix to be absent")
    if legacy.is_symlink() or not legacy.is_dir():
        pytest.fail("the fixed legacy external runtime is unavailable or aliased")

    legacy_info = legacy.stat()
    old_receipt_raw = legacy_receipt_path.read_text(encoding="utf-8")
    old_receipt = json.loads(old_receipt_raw)
    expected_old_receipt = {
        "runtime_kind": spec.EXTERNAL_KIND,
        "schema": spec.RECEIPT_SCHEMA,
        "vibecad_version": spec.VIBECAD_VERSION,
    }
    if old_receipt != expected_old_receipt:
        pytest.fail("legacy runtime does not carry the reviewed pre-epoch external receipt")
    if old_receipt_raw != json.dumps(old_receipt, sort_keys=True):
        pytest.fail("legacy pre-epoch external receipt is not canonical")

    binding_raw = binding_path.read_text(encoding="utf-8")
    old_binding = json.loads(binding_raw)
    if (
        old_binding.get("prefix") != str(legacy.resolve())
        or old_binding.get("prefix_device") != legacy_info.st_dev
        or old_binding.get("prefix_inode") != legacy_info.st_ino
        or any(
            key in old_binding
            for key in ("server_package_epoch", "mcp_version", "public_surface_sha256")
        )
    ):
        pytest.fail("external binding is not the reviewed identity-bound pre-epoch shape")
    if status.legacy_external_receipt(legacy) is not None:
        pytest.fail("pre-epoch external receipt must not satisfy the current strict contract")
    if status.runtime_receipt_state() is not status.ReceiptState.INCOMPATIBLE:
        pytest.fail("pre-epoch external binding must fail closed before migration")
    if status.runtime_ready():
        pytest.fail("pre-epoch external binding must not be reported ready")

    legacy_before = _tree_content_snapshot(legacy)
    binding_before = _single_file_snapshot(binding_path)
    data_before = _tree_content_snapshot(paths.data_root())
    phases: list[str] = []

    # The fixture already owns the non-reentrant maintenance lock. Calling the
    # locked implementation closes the race between the absent-prefix preflight,
    # the preservation snapshots, and installer admission.
    RuntimeInstaller(on_progress=lambda value: phases.append(value.phase.value))._install_locked()

    current_python = paths.env_python_for(current)
    assert current.is_dir() and not current.is_symlink()
    assert paths.active_runtime_prefix().resolve(strict=True) == current.resolve(strict=True)
    assert status.read_prefix_receipt(current) == spec.expected_receipt()
    assert status.runtime_receipt_state() is status.ReceiptState.CURRENT
    assert status.runtime_ready() is True
    assert status.verify_runtime(current_python) is True
    assert phases and phases[-1] == "ready"

    identity_script = (
        "import importlib.metadata as m,json,pathlib,vibecad;"
        "from vibecad.runtime import spec;"
        "print(json.dumps({'file':str(pathlib.Path(vibecad.__file__).resolve()),"
        "'vibecad':m.version('vibecad'),'mcp':m.version('mcp'),"
        "'epoch':spec.SERVER_PACKAGE_EPOCH,'surface':spec.PUBLIC_SURFACE_SHA256}))"
    )
    identity = subprocess.run(
        [str(current_python), "-I", "-B", "-c", identity_script],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
        env=status.freecad_process_environment(os.environ),
    )
    assert identity.returncode == 0, identity.stderr
    installed = json.loads(identity.stdout.strip().splitlines()[-1])
    assert Path(installed["file"]).is_relative_to(current)
    assert installed == {
        "file": installed["file"],
        "vibecad": spec.VIBECAD_VERSION,
        "mcp": spec.MCP_VERSION,
        "epoch": spec.SERVER_PACKAGE_EPOCH,
        "surface": spec.PUBLIC_SURFACE_SHA256,
    }

    assert _tree_content_snapshot(legacy) == legacy_before
    assert _single_file_snapshot(binding_path) == binding_before
    assert binding_path.read_text(encoding="utf-8") == binding_raw
    assert _tree_content_snapshot(paths.data_root()) == data_before


def test_walking_skeleton(runtime_env, tmp_path):
    """端到端 Walking Skeleton：建模→布尔→导出→诊断（真实 FreeCAD）。"""
    out = str(tmp_path)
    code = (
        status._PREP
        + f"import sys; sys.path.insert(0, {_SRC!r})\n"
        + "import os\n"
        + "from vibecad.engine.session import Session\n"
        + "from vibecad.tools import modeling, export\n"
        + "from vibecad.feedback.text import describe_shape\n"
        + "s = Session(); modeling.new_document(s, 'WalkingSkeleton')\n"
        + "box = modeling.add_box(s, 30, 30, 30)\n"
        + "assert box['ok'] and abs(box['volume'] - 27000) < 1e-2, box\n"
        + "cyl = modeling.add_cylinder(s, 8, 40)\n"
        + "assert cyl['ok'] and cyl['volume'] > 0\n"
        + "cut = modeling.boolean_cut(s, box['name'], cyl['name'])\n"
        + "assert cut['ok'] and 0 < cut['volume'] < 27000, cut\n"
        + f"r = export.export_part(s, {out!r})\n"
        + "assert os.path.getsize(r['step']) > 0 and os.path.getsize(r['stl']) > 0\n"
        + "d = describe_shape(s.get_object(cut['name']).Shape)\n"
        + "assert d['valid'] and d['solid_count'] == 1\n"
        + "assert abs(d['volume'] - cut['volume']) < 1e-4\n"
        + "print('SKELETON_OK')\n"
    )
    p = subprocess.run(
        [runtime_env, "-c", code],
        capture_output=True,
        text=True,
        timeout=180,
        env=status.freecad_process_environment(os.environ),
    )
    assert p.returncode == 0, p.stderr
    assert "SKELETON_OK" in p.stdout


def test_render_and_gltf(runtime_env, tmp_path):
    """端到端视觉：建模→布尔→render_png(PNG)→export_gltf(glb)（真实 FreeCAD）。"""
    glb = str(tmp_path / "p.glb")
    code = (
        status._PREP
        + f"import sys; sys.path.insert(0, {_SRC!r})\n"
        + "from pathlib import Path\n"
        + "from pygltflib import GLTF2\n"
        + "from vibecad.engine.session import Session\n"
        + "from vibecad.tools import modeling\n"
        + "from vibecad.feedback.render import render_png\n"
        + "from vibecad.feedback.gltf import export_gltf\n"
        + "s = Session(); modeling.new_document(s, 'Visual')\n"
        + "b = modeling.add_box(s, 30, 20, 10)\n"
        + "c = modeling.add_cylinder(s, 5, 30)\n"
        + "cut = modeling.boolean_cut(s, b['name'], c['name'])\n"
        + "shape = s.get_object(cut['name']).Shape\n"
        + "png = render_png(shape, view='iso')\n"
        + "assert png[:4] == b'\\x89PNG' and len(png) > 2000, len(png)\n"
        + f"gp = export_gltf(shape, {glb!r})\n"
        + "g = GLTF2().load(gp)\n"
        + "assert Path(gp).stat().st_size > 0 and len(g.meshes[0].primitives) > 0\n"
        + "print('VISUAL_OK')\n"
    )
    pr = subprocess.run(
        [runtime_env, "-c", code],
        capture_output=True,
        text=True,
        timeout=180,
        env=status.freecad_process_environment(os.environ),
    )
    assert pr.returncode == 0, pr.stderr
    assert "VISUAL_OK" in pr.stdout


def test_positioned_part(runtime_env, tmp_path):
    """端到端：position 参数造居中贯穿孔 + 渲染（真实 FreeCAD）。"""
    code = (
        status._PREP
        + f"import sys; sys.path.insert(0, {_SRC!r})\n"
        + "import math\n"
        + "from vibecad.engine.session import Session\n"
        + "from vibecad.tools import modeling\n"
        + "from vibecad.feedback.render import render_png\n"
        + "s = Session(); modeling.new_document(s, 'Plate')\n"
        + "b = modeling.add_box(s, 40, 30, 20)\n"
        + "c = modeling.add_cylinder(s, 6, 40, position=(20, 15, -10), axis='z')\n"
        + "cut = modeling.boolean_cut(s, b['name'], c['name'])\n"
        + "expected = 24000 - math.pi * 36 * 20\n"  # 居中贯穿 → 挖掉 box 内整段圆柱
        + "assert abs(cut['volume'] - expected) < 40, (cut['volume'], expected)\n"
        + "png = render_png(s.get_object(cut['name']).Shape)\n"
        + "assert png[:4] == b'\\x89PNG' and len(png) > 2000\n"
        + "print('POSITIONED_OK')\n"
    )
    pr = subprocess.run(
        [runtime_env, "-c", code],
        capture_output=True,
        text=True,
        timeout=180,
        env=status.freecad_process_environment(os.environ),
    )
    assert pr.returncode == 0, pr.stderr
    assert "POSITIONED_OK" in pr.stdout


def test_annotated_feature_flow(runtime_env, tmp_path):
    """端到端指代闭环（Round 5）：标注→顶面标签打孔→重新标注→导出 STEP（真实 FreeCAD）。"""
    out = str(tmp_path)
    code = (
        status._PREP
        + f"import sys; sys.path.insert(0, {_SRC!r})\n"
        + "import math, os\n"
        + "from vibecad.engine.session import Session\n"
        + "from vibecad.feedback.annotate import render_annotated\n"
        + "from vibecad.tools import export, features, modeling\n"
        + "s = Session(); modeling.new_document(s, 'R5Flow')\n"
        + "modeling.add_box(s, 40, 30, 20)\n"
        + "png, table, freg, ereg = render_annotated(s.get_result_shape(), mode='faces')\n"
        + "assert png[:4] == b'\\x89PNG' and len(png) > 2000, len(png)\n"
        + "s.set_labels(freg, ereg)\n"
        + "top = next(lab for lab, d in table.items() if '顶面' in d)\n"
        + "r = features.add_hole(s, top, 8)\n"
        + "expected = 24000 - math.pi * 16 * 20\n"  # ⌀8 通孔
        + "assert r['ok'] and abs(r['volume'] - expected) < 1.0, (r['volume'], expected)\n"
        + "png2, t2, freg2, ereg2 = render_annotated(s.get_result_shape(), mode='faces')\n"
        + "assert len(s.get_result_shape().Faces) > 6, len(s.get_result_shape().Faces)\n"
        + f"e = export.export_part(s, {out!r}, fmt='step')\n"
        + "assert os.path.getsize(e['step']) > 0\n"
        + "print('R5_FLOW_OK')\n"
    )
    p = subprocess.run(
        [runtime_env, "-c", code],
        capture_output=True,
        text=True,
        timeout=180,
        env=status.freecad_process_environment(os.environ),
    )
    assert p.returncode == 0, p.stderr
    assert "R5_FLOW_OK" in p.stdout


# --- Task 6：换芯端到端（C 分支：监督进程，同一管道零感知） ---
#
# 与上面几条慢测不同，这里不经 runtime_env 拿到的 conda python 直接 -c 跑代码
# （那条路径完全绕开 server.py 的 MCP 工具层，永远碰不到 _try_schedule_swap），
# 而是真起 `python -m vibecad`（宿主解释器）→ launcher.main → supervisor.Supervisor
# 常驻 → 首次 spawn 走 bootstrap 解释器（就绪哨兵此刻被藏起，_server_cmd 判定
# runtime_swappable()=False）。走完 initialize 握手后恢复哨兵，再调
# get_runtime_status 触发 server 侧 _try_schedule_swap → 1 秒后 os._exit(75) →
# supervisor 见 SWAP_EXIT 换 conda python 重启子进程 + 重放握手——全程只有一条
# stdio 管道，客户端（这条测试）不重连、不重新握手，直接在同一管道上继续调用。


def _read_line(proc: subprocess.Popen, timeout: float = 15.0) -> bytes:
    """带超时读一行：换芯/握手卡死时测试快速失败而非挂起整个 CI（对齐
    test_supervisor.py 的 _readline 兜底范式）。"""
    box: dict[str, bytes] = {}
    t = threading.Thread(target=lambda: box.setdefault("line", proc.stdout.readline()), daemon=True)
    t.start()
    t.join(timeout)
    if "line" not in box:
        proc.kill()
        pytest.fail(f"supervisor 未在 {timeout}s 内产出响应行（可能卡死于换芯/握手）")
    return box["line"]


def _send(proc: subprocess.Popen, obj: dict) -> None:
    proc.stdin.write(json.dumps(obj).encode() + b"\n")
    proc.stdin.flush()


def _rpc(
    proc: subprocess.Popen, id_: int, method: str, params: dict | None = None, timeout: float = 15.0
) -> dict:
    _send(proc, {"jsonrpc": "2.0", "id": id_, "method": method, "params": params or {}})
    return json.loads(_read_line(proc, timeout))


def _call_tool(proc: subprocess.Popen, id_: int, name: str, timeout: float = 15.0) -> dict:
    """封装 tools/call：返回顶层 JSON-RPC 响应（result/error）。"""
    return _rpc(proc, id_, "tools/call", {"name": name, "arguments": {}}, timeout=timeout)


def _tool_result_dict(resp: dict) -> dict:
    """FastMCP 的 tools/call 成功响应结构为
    {"result": {"content": [...], "structuredContent": {...} 或 isError}}——
    我们的工具都返回 dict，取 structuredContent（新版 SDK）或退回解析
    content[0].text 的 JSON（对齐 FastMCP 序列化两种可能形态，谁在场就用谁）。"""
    result = resp["result"]
    assert not result.get("isError"), resp
    if "structuredContent" in result:
        return result["structuredContent"]
    text = result["content"][0]["text"]
    return json.loads(text)


def _call_tool_with_arguments(
    proc: subprocess.Popen,
    id_: int,
    name: str,
    arguments: dict[str, object],
    *,
    timeout: float = 180.0,
) -> dict:
    return _rpc(
        proc,
        id_,
        "tools/call",
        {"name": name, "arguments": arguments},
        timeout=timeout,
    )


def _successful_public_result(response: dict) -> dict[str, object]:
    envelope = _tool_result_dict(response)
    assert envelope["ok"] is True, envelope
    assert envelope["error"] is None, envelope
    result = envelope["result"]
    assert type(result) is dict
    return result


def _mcpb_launch(unpacked: Path) -> tuple[list[str], dict[str, str], dict[str, object]]:
    manifest = json.loads((unpacked / "manifest.json").read_text(encoding="utf-8"))
    config = manifest["server"]["mcp_config"]
    executable = shutil.which(config["command"])
    if executable is None:
        pytest.fail(f"MCPB command is unavailable: {config['command']}")
    arguments = [str(unpacked) if item == "${__dirname}" else item for item in config["args"]]
    assert arguments[-1] == manifest["server"]["entry_point"]

    environment = status.freecad_process_environment(os.environ)
    environment.pop("PYTHONPATH", None)
    environment.pop("PYTHONHOME", None)
    environment.pop("VIRTUAL_ENV", None)
    environment.pop("UV_PROJECT_ENVIRONMENT", None)
    environment.update(config.get("env", {}))
    extra_raw = os.environ.get("VIBECAD_MCPB_EXTRA_ENV_JSON")
    if extra_raw:
        extra = json.loads(extra_raw)
        if type(extra) is not dict or any(
            type(key) is not str or type(value) is not str for key, value in extra.items()
        ):
            pytest.fail("VIBECAD_MCPB_EXTRA_ENV_JSON must be a JSON string map")
        environment.update(extra)
    return [executable, *arguments], environment, manifest


@pytest.mark.skipif(not _RUN, reason="set VIBECAD_RUN_INTEGRATION=1")
def test_unpacked_mcpb_agent_first_stdio_acceptance(tmp_path):
    """Run the packed launch contract through real MCP stdio and artifact resources.

    Root invocation supplies ``VIBECAD_MCPB_UNPACKED_DIR`` after ``mcpb unpack``.
    The harness never calls ``RuntimeInstaller`` directly and refuses to start
    unless the exact current managed runtime is already ready; the packed
    manifest keeps its own idempotent auto-ensure setting unchanged.
    """

    selected = os.environ.get("VIBECAD_MCPB_UNPACKED_DIR")
    if not selected:
        pytest.skip("set VIBECAD_MCPB_UNPACKED_DIR to a freshly unpacked MCPB directory")
    unpacked = Path(selected).expanduser().resolve(strict=True)
    _assert_fresh_unpacked_package(unpacked)
    if paths.user_override_env() is not None:
        pytest.fail("the MCPB acceptance gate requires a managed runtime")
    if (
        paths.active_runtime_prefix().resolve(strict=False)
        != paths.env_prefix().resolve(strict=False)
        or not status.runtime_ready()
    ):
        pytest.fail("install and verify the current managed runtime before MCPB acceptance")
    child_home, child_prefix = _validated_mcpb_child_binding()

    command, environment, manifest = _mcpb_launch(unpacked)
    nonce = secrets.token_hex(16)
    canary = "S3_MCPB_SECRET_CANARY_DO_NOT_LOG_7f42"
    environment["VIBECAD_E2E_SECRET_CANARY"] = canary

    # Run a probe through the exact frozen/no-dev/no-editable uv prefix used by
    # the manifest, replacing only its entry-point command.  This proves the
    # server import comes from the unpacked environment's site-packages.
    identity_script = (
        "import importlib.metadata as m,json,pathlib,vibecad;"
        "from vibecad.runtime import spec as s;"
        "p=pathlib.Path(vibecad.__file__).resolve();"
        "print(json.dumps({'file':str(p),'mcp':m.version('mcp'),"
        "'vibecad':m.version('vibecad'),'epoch':s.SERVER_PACKAGE_EPOCH}))"
    )
    identity_command = [*command[:-1], "python", "-I", "-c", identity_script]
    identity = subprocess.run(
        identity_command,
        cwd=unpacked,
        env=environment,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    assert identity.returncode == 0, identity.stderr
    identity_payload = json.loads(identity.stdout.strip().splitlines()[-1])
    installed_file = Path(identity_payload["file"])
    assert installed_file.is_relative_to(unpacked)
    assert "site-packages" in installed_file.parts
    assert not installed_file.is_relative_to(unpacked / "src")
    assert identity_payload["mcp"] == "1.27.2"
    assert identity_payload["vibecad"] == manifest["version"]
    assert identity_payload["epoch"] == spec.SERVER_PACKAGE_EPOCH == 4
    assert canary not in identity.stderr

    # Enable root DEBUG without putting either the checkout or the unpacked
    # source tree on PYTHONPATH.  The identity probe above deliberately ran
    # before this test-only site hook, so package provenance remains independent.
    debug_hook = tmp_path / "debug-hook"
    debug_hook.mkdir(mode=0o700)
    debug_marker = tmp_path / "debug-logging-active"
    (debug_hook / "sitecustomize.py").write_text(
        "import logging\n"
        "from pathlib import Path\n"
        "logging.basicConfig(level=logging.DEBUG)\n"
        "logging.getLogger().setLevel(logging.DEBUG)\n"
        f"Path({str(debug_marker)!r}).write_text('DEBUG', encoding='utf-8')\n",
        encoding="utf-8",
    )
    environment["PYTHONPATH"] = str(debug_hook)

    stderr_path = tmp_path / "mcpb-server.stderr"
    stderr_stream = stderr_path.open("w+b")
    proc: subprocess.Popen | None = None
    try:
        proc = subprocess.Popen(
            command,
            cwd=unpacked,
            env=environment,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=stderr_stream,
        )
        initialized = _rpc(
            proc,
            0,
            "initialize",
            {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "vibecad-mcpb-acceptance", "version": "1.0.0"},
            },
            timeout=60.0,
        )
        assert initialized["result"]["serverInfo"] == {
            "name": "vibecad",
            "version": manifest["version"],
        }
        _send(proc, {"jsonrpc": "2.0", "method": "notifications/initialized"})

        _send(proc, {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}})
        tools_frame = _read_line(proc, 60.0)
        assert tools_frame.endswith(b"\n")
        assert len(tools_frame) <= 65_536
        tools_response = json.loads(tools_frame)
        assert tools_frame == (
            json.dumps(
                tools_response,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
            + b"\n"
        )
        listed_tools = tools_response["result"]["tools"]
        expected_tools = _expected_tool_projection()
        assert tools_response["id"] == 1
        assert len(listed_tools) == 20
        assert [item["name"] for item in listed_tools] == [
            item["name"] for item in manifest["tools"]
        ]
        assert listed_tools == expected_tools
        assert all(
            type(item["description"]) is str
            and item["description"]
            and item["description"] == item["description"].strip()
            and "\n" not in item["description"]
            and "\r" not in item["description"]
            for item in listed_tools
        )
        assert all("outputSchema" not in item for item in listed_tools)
        assert _rpc(proc, 2, "resources/list")["result"]["resources"] == []
        templates = _rpc(proc, 3, "resources/templates/list")["result"]["resourceTemplates"]
        assert len(templates) == 1
        assert templates[0]["uriTemplate"].startswith("vibecad://artifact/")

        # A fresh MCPB home binds the existing engine in a background installer,
        # then the bootstrap server exits and the supervisor transparently swaps
        # to that runtime.  The control plane is intentionally available during
        # this window, while application calls fail closed.  Retry only this
        # request-keyed/idempotent create with the exact transient envelope: it
        # cannot duplicate the project across a lost response or swap replay.
        project_arguments = {
            "schema_version": 1,
            "create_key": f"project_create_{nonce}",
            "kind": "empty",
        }
        transient_runtime_error = {
            "schema_version": 1,
            "ok": False,
            "result": None,
            "error": {
                "schema_version": 1,
                "code": "runtime_unavailable",
                "path": "",
                "message": "The managed CAD runtime is not active.",
            },
        }
        deadline = time.monotonic() + 300.0
        request_id = 1000
        last_project_response: dict | None = None
        project: dict[str, object] | None = None
        while time.monotonic() < deadline:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            last_project_response = _call_tool_with_arguments(
                proc,
                request_id,
                "create_project",
                project_arguments,
                timeout=min(60.0, remaining),
            )
            if "error" in last_project_response:
                assert last_project_response == {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "error": {"code": -32005, "message": "Server is busy."},
                }
                request_id += 1
                remaining = deadline - time.monotonic()
                if remaining > 0:
                    time.sleep(min(0.25, remaining))
                continue
            wire_result = last_project_response["result"]
            if wire_result.get("isError") is not True:
                project = _successful_public_result(last_project_response)
                break
            assert wire_result["structuredContent"] == transient_runtime_error
            request_id += 1
            remaining = deadline - time.monotonic()
            if remaining > 0:
                time.sleep(min(0.25, remaining))
        assert project is not None, (
            f"packaged runtime did not finish binding and swap within 300s: {last_project_response}"
        )
        project_id = project["project_id"]
        task = _successful_public_result(
            _call_tool_with_arguments(
                proc,
                5,
                "create_task",
                {
                    "schema_version": 1,
                    "project_id": project_id,
                    "review_policy": "auto_commit",
                },
            )
        )
        task_id = task["task_run"]["id"]
        acceptance = json.dumps(
            {
                "schema_version": 1,
                "id": "mcpb-real-acceptance",
                "criteria": [
                    {
                        "schema_version": 1,
                        "id": "valid-shape",
                        "kind": "topology",
                        "check": "valid_shape",
                        "target": "body",
                        "expected": True,
                        "tolerance": None,
                        "parameters": {},
                        "required": True,
                    }
                ],
            },
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        terminal_response = _call_tool_with_arguments(
            proc,
            6,
            "create_box",
            {
                "schema_version": 1,
                "task_id": task_id,
                "expected_generation": task["generation"],
                "target": {},
                "arguments": {
                    "length_mm": 10,
                    "width_mm": 20,
                    "height_mm": 30,
                    "position_mm": [0, 0, 0],
                },
                "preserve": [],
                "acceptance_json": acceptance,
            },
            timeout=300.0,
        )
        terminal = _successful_public_result(terminal_response)
        assert [item["type"] for item in terminal_response["result"]["content"]] == ["text"]
        assert terminal["task_run"]["status"] == "succeeded"
        committed_revision = terminal["task_run"]["committed_revision"]
        export_response = _call_tool_with_arguments(
            proc,
            7,
            "export_task_artifacts",
            {
                "schema_version": 1,
                "export_key": f"export_{nonce}",
                "task_id": task_id,
                "expected_generation": terminal["generation"],
                "revision_id": committed_revision,
                "draft_id": None,
            },
            timeout=300.0,
        )
        exported = _successful_public_result(export_response)
        assert exported["source_kind"] == "committed"
        assert [item["format"] for item in exported["artifacts"]] == ["fcstd", "step"]
        export_wire = export_response["result"]
        assert export_wire["content"][0] == {
            "type": "text",
            "text": json.dumps(
                export_wire["structuredContent"],
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
                sort_keys=True,
            ),
        }
        assert export_wire["content"][1:] == [
            {
                "type": "resource_link",
                "name": exported["artifacts"][0]["name"],
                "uri": exported["artifacts"][0]["resource_uri"],
                "mimeType": "application/vnd.freecad.fcstd",
                "size": exported["artifacts"][0]["size_bytes"],
            },
            {
                "type": "resource_link",
                "name": exported["artifacts"][1]["name"],
                "uri": exported["artifacts"][1]["resource_uri"],
                "mimeType": "model/step",
                "size": exported["artifacts"][1]["size_bytes"],
            },
        ]
        expected_mime_types = {
            "fcstd": "application/vnd.freecad.fcstd",
            "step": "model/step",
        }
        for offset, artifact in enumerate(exported["artifacts"], start=8):
            resource = _rpc(
                proc,
                offset,
                "resources/read",
                {"uri": artifact["resource_uri"]},
                timeout=180.0,
            )["result"]["contents"]
            assert len(resource) == 1
            content = resource[0]
            assert content["uri"] == artifact["resource_uri"]
            assert content["mimeType"] == expected_mime_types[artifact["format"]]
            raw = base64.b64decode(content["blob"], validate=True)
            assert len(raw) == artifact["size_bytes"]
            assert hashlib.sha256(raw).hexdigest() == artifact["sha256"]

        failed_export = _call_tool_with_arguments(
            proc,
            10,
            "export_task_artifacts",
            {
                "schema_version": 1,
                "export_key": f"export_failed_{nonce}",
                "task_id": task_id,
                "expected_generation": terminal["generation"] + 1,
                "revision_id": committed_revision,
                "draft_id": None,
            },
            timeout=180.0,
        )
        failed_export_result = failed_export["result"]
        assert failed_export_result["isError"] is True
        assert failed_export_result["structuredContent"]["ok"] is False
        assert [item["type"] for item in failed_export_result["content"]] == ["text"]

        # Real DEBUG-mode negative traffic must remain fixed and path/input free.
        # Keep every request on the live packed stdio path: protocol framing,
        # tool prevalidation/ingress and owned resource grammar are distinct seams.
        negative_responses = []
        _send(
            proc,
            {
                "jsonrpc": "2.0",
                "id": 20,
                "method": "ping",
                "params": {},
                f"{canary}_protocol_key": canary,
            },
        )
        negative_responses.append(json.loads(_read_line(proc, 60.0)))
        assert negative_responses[-1] == {
            "jsonrpc": "2.0",
            "id": 20,
            "error": {"code": -32600, "message": "Invalid Request"},
        }

        unknown_tool = _call_tool_with_arguments(proc, 21, canary, {"secret": canary})
        negative_responses.append(unknown_tool)
        assert unknown_tool == {
            "jsonrpc": "2.0",
            "id": 21,
            "error": {"code": -32602, "message": "Tool name is not available."},
        }

        invalid_tool = _call_tool_with_arguments(proc, 22, "ping", {canary: canary})
        negative_responses.append(invalid_tool)
        invalid_result = invalid_tool["result"]
        assert invalid_result["isError"] is True
        assert invalid_result["structuredContent"]["error"] == {
            "schema_version": 1,
            "code": "unknown_field",
            "path": "/_unknown",
            "message": "The request contains an unknown field.",
        }

        invalid_resource = _rpc(
            proc,
            23,
            "resources/read",
            {"uri": f"file:///{canary}"},
            timeout=60.0,
        )
        negative_responses.append(invalid_resource)
        assert invalid_resource == {
            "jsonrpc": "2.0",
            "id": 23,
            "error": {
                "code": -32602,
                "message": "Artifact resource identifier is invalid.",
            },
        }

        removed_legacy_tool = _call_tool_with_arguments(proc, 24, "add_box", {})
        negative_responses.append(removed_legacy_tool)
        assert removed_legacy_tool == {
            "jsonrpc": "2.0",
            "id": 24,
            "error": {"code": -32602, "message": "Tool name is not available."},
        }
        assert canary not in json.dumps(
            negative_responses,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )

        proc.stdin.close()
        assert proc.wait(timeout=30) == 0
        proc = None
    finally:
        if proc is not None and proc.poll() is None:
            proc.kill()
            with contextlib.suppress(Exception):
                proc.wait(timeout=10)
        stderr_stream.close()

    stderr = stderr_path.read_text(encoding="utf-8", errors="replace")
    assert debug_marker.read_text(encoding="utf-8") == "DEBUG"
    assert canary not in stderr

    child_info = child_home.lstat()
    assert stat.S_ISDIR(child_info.st_mode) and not child_home.is_symlink()
    assert child_home.resolve(strict=True) == child_home
    assert child_info.st_uid == os.getuid()
    assert stat.S_IMODE(child_info.st_mode) & 0o077 == 0
    prefix_info = child_prefix.stat()
    expected_external_receipt = {
        **spec.expected_receipt(external=True),
        "prefix": str(child_prefix),
        "prefix_device": prefix_info.st_dev,
        "prefix_inode": prefix_info.st_ino,
        "python_version": ".".join(map(str, spec.PYTHON_VERSION)),
        "freecad_version": ".".join(map(str, spec.FREECAD_VERSION)),
    }
    receipt_path = child_home / "runtime" / "external-runtime.json"
    assert receipt_path.is_file() and not receipt_path.is_symlink()
    receipt_raw = receipt_path.read_text(encoding="utf-8")
    assert receipt_raw == json.dumps(
        expected_external_receipt,
        sort_keys=True,
    )


@pytest.mark.slow
def test_auto_swap_end_to_end(runtime_env, tmp_path):
    """换芯全链路（C 分支）：把就绪哨兵临时藏起 → 起 launcher（bootstrap 芯）→
    真实 MCP 握手 → 调 ping 确认 bootstrap 芯工作 → 恢复哨兵 → 调
    get_runtime_status 触发 _try_schedule_swap → server 1 秒后 exit(75) →
    supervisor 换芯重启进 conda 芯 + 握手重放（轮询直至新芯接管）→ 同一管道继续
    调 smoke_cad 成功——零感知换芯的最终证据。打印 SWAP_OK。
    """
    sentinel = paths.ready_sentinel()
    hidden = sentinel.with_name(sentinel.name + ".hidden-by-swap-test")
    assert sentinel.exists(), "runtime_env 应已保证就绪哨兵存在"

    # 步骤 1：把哨兵临时藏起，伪装"运行时未就绪"——逼 supervisor 首次 spawn 落
    # bootstrap 解释器（runtime_swappable()=False）。试结束必须恢复，用 try/finally。
    sentinel.rename(hidden)
    proc: subprocess.Popen | None = None
    try:
        # 步骤 2：subprocess 起 launcher（python -m vibecad，宿主解释器），stdio 管道。
        # 环境变量对齐 runtime_env fixture 已设置的 VIBECAD_HOME/VIBECAD_FREECAD_ENV
        # （os.environ 继承）；不开 VIBECAD_AUTO_INSTALL——运行时已就绪，不需要触发
        # 真实（重）安装，纯靠恢复哨兵 + get_runtime_status 驱动换芯。
        env = status.freecad_process_environment(os.environ)
        env.pop("VIBECAD_AUTO_INSTALL", None)
        # 开发态 launcher 应明确从当前 checkout 启动，不依赖 editable .pth 是否被
        # macOS/uv 标记 hidden；发布安装态则由已安装 wheel 提供同一包。
        env["PYTHONPATH"] = os.pathsep.join(part for part in (_SRC, env.get("PYTHONPATH")) if part)
        proc = subprocess.Popen(
            [sys.executable, "-m", "vibecad"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            env=env,
        )

        # 真实 MCP JSON-RPC 行协议握手：initialize 带完整 params，收到响应后发
        # notifications/initialized（无 id，通知不等响应）。
        init_resp = _rpc(
            proc,
            0,
            "initialize",
            {
                "protocolVersion": "2025-11-25",
                "capabilities": {},
                "clientInfo": {"name": "vibecad-swap-test", "version": "0.0.0"},
            },
        )
        assert "result" in init_resp, init_resp
        _send(proc, {"jsonrpc": "2.0", "method": "notifications/initialized"})

        # 步骤 3：调 ping 确认 bootstrap 芯工作（bootstrap = 宿主解释器，非 conda）。
        ping_resp = _call_tool(proc, 1, "ping")
        assert "vibecad ok" in json.dumps(ping_resp), ping_resp

        # 步骤 4：恢复哨兵——运行时"变为"就绪，供下一步触发换芯判据。
        hidden.rename(sentinel)

        # 步骤 5：调 get_runtime_status → 触发 server 侧 _try_schedule_swap →
        # 1 秒后 os._exit(75) → supervisor 换芯重启进 conda 芯 + 握手重放。
        # 轮询而非裸 sleep：反复调 get_runtime_status，直到 needs_reconnect=False
        # 且 phase=ready（新芯已接管、同一管道继续可用）或超时。
        status_resp = _call_tool(proc, 2, "get_runtime_status")
        status_dict = _tool_result_dict(status_resp)
        assert status_dict["phase"] == "ready", status_dict
        assert status_dict["needs_reconnect"] is False, status_dict  # C 分支恒 False

        # 步骤 5+6 合并：以 smoke_cad 本身为换芯完成的探针——bootstrap 芯的
        # smoke_cad 被 guard 拦截（ok:False"正在自动切换"），conda 芯才能真跑
        # FreeCAD 成功。不能用"get_runtime_status 响应可解析"当判据：自退 Timer
        # 有 1 秒延迟，旧 bootstrap 芯在退出前照常响应，会被误判为换芯完成。
        # 同一管道轮询到 ok:True 即零感知换芯的最终证据（从未重连、从未重新握手）。
        deadline = time.monotonic() + 150.0  # 换芯 + 首次 import FreeCAD 都在窗口内
        next_id = 3
        smoke_dict: dict | None = None
        last_err: Exception | None = None
        while time.monotonic() < deadline:
            try:
                resp = _call_tool(proc, next_id, "smoke_cad", timeout=90.0)
                candidate = _tool_result_dict(resp)
                if candidate.get("ok") is True:
                    smoke_dict = candidate
                    break
                last_err = AssertionError(f"仍在 bootstrap 芯：{candidate}")
            except Exception as exc:  # noqa: BLE001 - 换芯窗口内瞬时失败属预期，继续轮询
                last_err = exc
            finally:
                next_id += 1
            time.sleep(1.0)
        assert smoke_dict is not None, f"150s 内未完成零感知换芯：{last_err}"
        assert abs(smoke_dict["volume"] - 1000.0) < 1e-6, smoke_dict

        proc.stdin.close()
        assert proc.wait(timeout=15) == 0
        proc = None
        print("SWAP_OK")
    finally:
        # 全程读写要有超时兜底，绝不挂死：无论中途在哪一步失败都要收尾子进程。
        if proc is not None and proc.poll() is None:
            proc.kill()
            with contextlib.suppress(Exception):
                proc.wait(timeout=10)
        # 哨兵必须恢复：不管测试成功/失败，绝不留一个"哨兵被藏起"的持久 env
        # 给后续测试或用户的真实运行时用。
        if hidden.exists() and not sentinel.exists():
            hidden.rename(sentinel)
