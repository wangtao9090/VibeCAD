"""标记/直删/护栏。全部 monkeypatch VIBECAD_HOME → tmp，不碰真实目录。

例外：test_refuses_home_dir / test_refuses_root 故意指向危险路径——
被测行为正是护栏拒删，全程不应有任何删除发生。
"""

import hashlib
import json
import stat
import threading
import time
from pathlib import Path

import pytest

from vibecad.runtime import spec, status, uninstall


def _fingerprint(root: Path) -> dict[str, tuple[int, int, int, str | None]]:
    found = {}
    if not root.exists():
        return found
    for path in [root, *sorted(root.rglob("*"))]:
        info = path.lstat()
        digest = None
        if stat.S_ISREG(info.st_mode):
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
        found[str(path.relative_to(root))] = (
            info.st_ino,
            stat.S_IFMT(info.st_mode),
            info.st_size,
            digest,
        )
    return found


def test_request_marks_and_perform_deletes(monkeypatch, tmp_path):
    home = tmp_path / "home"
    (home / "runtime").mkdir(parents=True)
    (home / "runtime" / "big.bin").write_bytes(b"x" * 1024)
    monkeypatch.setenv("VIBECAD_HOME", str(home))
    assert uninstall.request_uninstall()["marked"]
    assert uninstall.perform_pending_uninstall() is True
    assert not (home / "runtime").exists()
    assert not home.exists(), "authorized cleanup may remove the now-empty container"


def test_perform_noop_without_marker(monkeypatch, tmp_path):
    monkeypatch.setenv("VIBECAD_HOME", str(tmp_path))
    assert uninstall.perform_pending_uninstall() is False
    assert tmp_path.exists()


def test_perform_noop_without_marker_does_not_create_missing_home(monkeypatch, tmp_path):
    home = tmp_path / "missing-home"
    monkeypatch.setenv("VIBECAD_HOME", str(home))
    assert uninstall.perform_pending_uninstall() is False
    assert not home.exists()


def test_uninstall_now_reports_size(monkeypatch, tmp_path):
    home = tmp_path / "home"
    (home / "runtime").mkdir(parents=True)
    (home / "runtime" / "f.bin").write_bytes(b"x" * 2048)
    monkeypatch.setenv("VIBECAD_HOME", str(home))
    info = uninstall.uninstall_now()
    assert info["ok"] and not (home / "runtime").exists() and info["freed_mb"] >= 0


def test_uninstall_now_message_scales_units(monkeypatch, tmp_path):
    """小体量显示 MB 而非 '0.0 GB'（模拟测试逮到的文案问题）。"""
    home = tmp_path / "home"
    (home / "runtime").mkdir(parents=True)
    (home / "runtime" / "f.bin").write_bytes(b"x" * (5 * 1024 * 1024))
    monkeypatch.setenv("VIBECAD_HOME", str(home))
    info = uninstall.uninstall_now()
    assert info["ok"] and "MB" in info["message"] and "0.0 GB" not in info["message"]


def test_override_env_never_deleted(monkeypatch, tmp_path):
    """VIBECAD_FREECAD_ENV 用户自带 env 在 home 之外——删除 home 不得波及。"""
    home, override = tmp_path / "home", tmp_path / "user-env"
    (home / "runtime").mkdir(parents=True)
    override.mkdir()
    (override / "keep").write_text("x")
    monkeypatch.setenv("VIBECAD_HOME", str(home))
    monkeypatch.setenv("VIBECAD_FREECAD_ENV", str(override))
    uninstall.request_uninstall()
    uninstall.perform_pending_uninstall()
    assert not (home / "runtime").exists()
    assert (override / "keep").exists()


def test_refuses_home_dir(monkeypatch):
    monkeypatch.setenv("VIBECAD_HOME", "~")
    assert uninstall.uninstall_now()["ok"] is False


def test_refuses_root(monkeypatch):
    monkeypatch.setenv("VIBECAD_HOME", "/")
    assert uninstall.uninstall_now()["ok"] is False


def test_refuses_dir_without_sentinel(monkeypatch, tmp_path):
    """深路径但无 VibeCAD 安装产物（如用户文档目录）→ 拒删且文件无恙。"""
    d = tmp_path / "a" / "b" / "Documents"
    d.mkdir(parents=True)
    (d / "important.txt").write_text("user data")
    monkeypatch.setenv("VIBECAD_HOME", str(d))
    info = uninstall.uninstall_now()
    assert info["ok"] is True and info["already_clean"] is True
    assert (d / "important.txt").exists()


def test_request_refuses_unsafe_home_before_marking(monkeypatch, tmp_path):
    """request 也要在写标记前拦截（不能给被污染的目录埋雷）。"""
    d = tmp_path / "x" / "y" / "Docs"
    d.mkdir(parents=True)
    (d / "f.txt").write_text("z")
    monkeypatch.setenv("VIBECAD_HOME", str(d))
    info = uninstall.request_uninstall()
    assert info["ok"] is True and info["already_clean"] is True
    assert not (d / ".uninstall_requested").exists()


def test_mcpb_runtime_path_allowed(monkeypatch, tmp_path):
    """Old MCPB-shaped home is ambiguous and therefore preserved."""
    d = tmp_path / "Claude Extensions" / "local.mcpb.x" / "runtime"
    d.mkdir(parents=True)
    (d / "status.json").write_text("{}")
    monkeypatch.setenv("VIBECAD_HOME", str(d))
    info = uninstall.uninstall_now()
    assert info["ok"] is True and info["already_clean"] is True and d.exists()


def test_empty_dir_allowed(monkeypatch, tmp_path):
    """空 home（已清洁/未安装）→ 放行删除无危害。"""
    d = tmp_path / "deep" / "VibeCADHome"
    d.mkdir(parents=True)
    monkeypatch.setenv("VIBECAD_HOME", str(d))
    assert uninstall.uninstall_now()["ok"] is True


def test_refuses_user_project_with_bin(monkeypatch, tmp_path):
    """含裸 bin/ 的用户项目 → 拒删，源码无恙（特征清单收紧的回归锁）。"""
    d = tmp_path / "deep" / "myproject"
    (d / "bin").mkdir(parents=True)
    (d / "src").mkdir()
    (d / "src" / "main.c").write_text("int main(){}")
    monkeypatch.setenv("VIBECAD_HOME", str(d))
    assert uninstall.uninstall_now()["already_clean"] is True
    assert (d / "src" / "main.c").exists()


def test_refuses_web_project_with_views(monkeypatch, tmp_path):
    d = tmp_path / "deep" / "webapp"
    (d / "views").mkdir(parents=True)
    (d / "app.py").write_text("app")
    monkeypatch.setenv("VIBECAD_HOME", str(d))
    assert uninstall.uninstall_now()["already_clean"] is True
    assert (d / "app.py").exists()


def test_refuses_symlink_home(monkeypatch, tmp_path):
    real = tmp_path / "real-dir"
    real.mkdir()
    (real / "data.txt").write_text("x")
    link = tmp_path / "link"
    link.symlink_to(real)
    monkeypatch.setenv("VIBECAD_HOME", str(link))
    assert uninstall.uninstall_now()["ok"] is False
    assert (real / "data.txt").exists()


def test_managed_bin_micromamba_allowed(monkeypatch, tmp_path):
    """A bare legacy micromamba is not ownership proof and must survive."""
    d = tmp_path / "deep" / "runtime-home"
    (d / "bin").mkdir(parents=True)
    (d / "bin" / "micromamba").write_bytes(b"\x7fELF")
    monkeypatch.setenv("VIBECAD_HOME", str(d))
    info = uninstall.uninstall_now()
    assert info["ok"] is True and info["already_clean"] is True and d.exists()


def test_uninstall_now_removes_only_runtime_and_preserves_durable_content(monkeypatch, tmp_path):
    home = tmp_path / "VibeCAD"
    runtime = home / "runtime"
    data = home / "data"
    views = home / "views"
    unknown = home / "keep-me.txt"
    (runtime / "mamba" / "envs" / "vibecad").mkdir(parents=True)
    (runtime / "engine.bin").write_bytes(b"runtime bytes")
    (data / "projects" / "p1").mkdir(parents=True)
    (data / "projects" / "p1" / "HEAD").write_bytes(b"durable head")
    (data / "artifacts" / "requests").mkdir(parents=True)
    (data / "artifacts" / "requests" / "export.json").write_bytes(b"durable export")
    (data / "artifacts" / "materializations" / "m1").mkdir(parents=True)
    (data / "artifacts" / "materializations" / "m1" / "model.FCStd").write_bytes(b"durable model")
    (data / "artifacts" / "materializations" / "m1" / "model.step").write_bytes(b"durable step")
    views.mkdir()
    (views / "preview.png").write_bytes(b"png")
    unknown.write_text("unknown", encoding="utf-8")
    monkeypatch.setenv("VIBECAD_HOME", str(home))
    before = _fingerprint(home)

    info = uninstall.uninstall_now()

    assert info["ok"] is True and info["data_preserved"] is True
    assert not runtime.exists()
    assert home.exists()
    after = _fingerprint(home)
    for relative, evidence in before.items():
        if relative == "runtime" or relative.startswith("runtime/") or relative == ".":
            continue
        assert after[relative] == evidence


def test_pending_uninstall_preserves_data_inode_and_clears_marker(monkeypatch, tmp_path):
    home = tmp_path / "VibeCAD"
    runtime = home / "runtime"
    data = home / "data"
    runtime.mkdir(parents=True)
    (runtime / "engine.bin").write_bytes(b"runtime")
    (data / "tasks").mkdir(parents=True)
    task = data / "tasks" / "task.json"
    task.write_bytes(b"task")
    monkeypatch.setenv("VIBECAD_HOME", str(home))
    before = _fingerprint(data)

    requested = uninstall.request_uninstall()
    assert requested["marked"] and requested["data_preserved"] is True
    assert uninstall.perform_pending_uninstall() is True

    assert not runtime.exists()
    assert _fingerprint(data) == before
    assert not uninstall.uninstall_marker().exists()


def test_preview_excludes_data_bytes_and_reports_preserved_ambiguous_legacy(monkeypatch, tmp_path):
    home = tmp_path / "VibeCAD"
    runtime = home / "runtime"
    legacy = home / "mamba" / "envs" / "vibecad"
    (runtime / "engine.bin").parent.mkdir(parents=True)
    (runtime / "engine.bin").write_bytes(b"r" * 10)
    (home / "data").mkdir()
    (home / "data" / "huge.fcstd").write_bytes(b"d" * 2_000_000)
    legacy.mkdir(parents=True)
    (legacy / "unknown.bin").write_bytes(b"legacy")
    monkeypatch.setenv("VIBECAD_HOME", str(home))
    data_before = _fingerprint(home / "data")
    legacy_before = _fingerprint(legacy)

    preview = uninstall.preview_uninstall()

    assert preview["data_preserved"] is True
    assert preview["size_mb"] < 0.01
    assert str(runtime) in preview["paths"]
    assert str(legacy) in preview["preserved_paths"]
    assert _fingerprint(home / "data") == data_before
    assert _fingerprint(legacy) == legacy_before


def test_exact_managed_legacy_is_authorized_but_unknown_siblings_survive(monkeypatch, tmp_path):
    home = tmp_path / "VibeCAD"
    legacy = home / "mamba" / "envs" / "vibecad"
    legacy.mkdir(parents=True)
    (legacy / ".vibecad_ready").write_text(
        json.dumps(spec.expected_receipt(), sort_keys=True), encoding="utf-8"
    )
    (legacy / "engine.bin").write_bytes(b"owned")
    unknown = home / "mamba" / "user-channel-cache.bin"
    unknown.write_bytes(b"keep")
    data = home / "data"
    data.mkdir()
    (data / "keep").write_bytes(b"data")
    monkeypatch.setenv("VIBECAD_HOME", str(home))
    data_before = _fingerprint(data)

    info = uninstall.uninstall_now()

    assert info["ok"] is True
    assert not legacy.exists()
    assert unknown.read_bytes() == b"keep"
    assert _fingerprint(data) == data_before


def test_managed_legacy_never_authorizes_ambiguous_home_fixed_files(
    monkeypatch,
    tmp_path,
):
    home = tmp_path / "VibeCAD"
    legacy = home / "mamba" / "envs" / "vibecad"
    legacy.mkdir(parents=True)
    (legacy / ".vibecad_ready").write_text(
        json.dumps(spec.expected_receipt(), sort_keys=True), encoding="utf-8"
    )
    status_file = home / "status.json"
    install_log = home / "install.log"
    install_lock = home / ".install.lock"
    status_file.write_bytes(b"user status")
    install_log.write_bytes(b"user log")
    install_lock.mkdir()
    (install_lock / "owner").write_bytes(b"user lock")
    monkeypatch.setenv("VIBECAD_HOME", str(home))

    info = uninstall.uninstall_now()

    assert info["ok"] is True
    assert not legacy.exists()
    assert status_file.read_bytes() == b"user status"
    assert install_log.read_bytes() == b"user log"
    assert (install_lock / "owner").read_bytes() == b"user lock"
    assert {str(status_file), str(install_log), str(install_lock)} <= set(info["preserved_paths"])


def test_runtime_data_alias_fails_closed(monkeypatch, tmp_path):
    home = tmp_path / "VibeCAD"
    data = home / "data"
    data.mkdir(parents=True)
    (data / "keep").write_bytes(b"important")
    (home / "runtime").symlink_to(data, target_is_directory=True)
    monkeypatch.setenv("VIBECAD_HOME", str(home))

    info = uninstall.uninstall_now()

    assert info["ok"] is False
    assert (data / "keep").read_bytes() == b"important"


def test_data_symlink_into_runtime_subtree_blocks_preview_direct_and_pending(monkeypatch, tmp_path):
    home = tmp_path / "VibeCAD"
    misplaced = home / "runtime" / "misplaced-durable"
    misplaced.mkdir(parents=True)
    head = misplaced / "HEAD.json"
    head.write_bytes(b"durable-head")
    (home / "data").symlink_to(misplaced, target_is_directory=True)
    monkeypatch.setenv("VIBECAD_HOME", str(home))
    before = _fingerprint(misplaced)

    assert uninstall.preview_uninstall()["ok"] is False
    assert uninstall.uninstall_now()["ok"] is False
    assert uninstall.request_uninstall()["ok"] is False
    assert uninstall.perform_pending_uninstall() is False

    assert _fingerprint(misplaced) == before
    assert head.read_bytes() == b"durable-head"
    assert not uninstall.uninstall_marker().exists()


def test_data_symlink_into_managed_legacy_blocks_every_uninstall_entrypoint(monkeypatch, tmp_path):
    home = tmp_path / "VibeCAD"
    legacy = home / "mamba" / "envs" / "vibecad"
    misplaced = legacy / "misplaced-durable"
    misplaced.mkdir(parents=True)
    (legacy / ".vibecad_ready").write_text(
        json.dumps(spec.expected_receipt(), sort_keys=True), encoding="utf-8"
    )
    head = misplaced / "HEAD.json"
    head.write_bytes(b"durable-head")
    (home / "data").symlink_to(misplaced, target_is_directory=True)
    monkeypatch.setenv("VIBECAD_HOME", str(home))
    before = _fingerprint(legacy)

    assert uninstall.preview_uninstall()["ok"] is False
    assert uninstall.uninstall_now()["ok"] is False
    assert uninstall.request_uninstall()["ok"] is False
    assert uninstall.perform_pending_uninstall() is False

    assert _fingerprint(legacy) == before
    assert head.read_bytes() == b"durable-head"
    assert not uninstall.uninstall_marker().exists()


def test_external_override_tree_is_untouched_by_direct_and_pending_uninstall(monkeypatch, tmp_path):
    home = tmp_path / "VibeCAD"
    override = tmp_path / "external"
    (override / "bin").mkdir(parents=True)
    (override / "bin" / "python").write_bytes(b"python")
    (override / "engine.bin").write_bytes(b"external")
    monkeypatch.setenv("VIBECAD_HOME", str(home))
    monkeypatch.setenv("VIBECAD_FREECAD_ENV", str(override))
    before = _fingerprint(override)

    (home / "runtime").mkdir(parents=True)
    uninstall.uninstall_now()
    assert _fingerprint(override) == before

    (home / "runtime").mkdir(parents=True)
    uninstall.request_uninstall()
    uninstall.perform_pending_uninstall()
    assert _fingerprint(override) == before


def test_explicit_legacy_override_survives_direct_and_pending_uninstall(monkeypatch, tmp_path):
    home = tmp_path / "VibeCAD"
    legacy = home / "mamba" / "envs" / "vibecad"
    legacy.mkdir(parents=True)
    (legacy / ".vibecad_ready").write_text(
        json.dumps(spec.expected_receipt(), sort_keys=True), encoding="utf-8"
    )
    (legacy / "engine.bin").write_bytes(b"explicit external runtime")
    monkeypatch.setenv("VIBECAD_HOME", str(home))
    monkeypatch.setenv("VIBECAD_FREECAD_ENV", str(legacy))
    before = _fingerprint(legacy)

    (home / "runtime").mkdir()
    assert uninstall.uninstall_now()["ok"] is True
    assert _fingerprint(legacy) == before

    (home / "runtime").mkdir()
    assert uninstall.request_uninstall()["marked"] is True
    assert uninstall.perform_pending_uninstall() is True
    assert _fingerprint(legacy) == before


def test_managed_legacy_cannot_acquire_conflicting_external_binding(monkeypatch, tmp_path):
    home = tmp_path / "VibeCAD"
    legacy = home / "mamba" / "envs" / "vibecad"
    (legacy / "bin").mkdir(parents=True)
    (legacy / "bin" / "python").write_bytes(b"python")
    (legacy / ".vibecad_ready").write_text(
        json.dumps(spec.expected_receipt(), sort_keys=True), encoding="utf-8"
    )
    monkeypatch.setenv("VIBECAD_HOME", str(home))
    monkeypatch.setenv("VIBECAD_FREECAD_ENV", str(legacy))
    before = _fingerprint(legacy)

    with pytest.raises(ValueError, match="conflicting managed ownership"):
        status.write_external_runtime_receipt(legacy)

    assert _fingerprint(legacy) == before
    assert not status.paths.external_runtime_receipt().exists()


def test_identity_bound_external_kind_legacy_survives_after_override_is_unset(
    monkeypatch, tmp_path
):
    home = tmp_path / "VibeCAD"
    legacy = home / "mamba" / "envs" / "vibecad"
    (legacy / "bin").mkdir(parents=True)
    (legacy / "bin" / "python").write_bytes(b"python")
    (legacy / ".vibecad_ready").write_text(
        json.dumps(spec.expected_receipt(external=True), sort_keys=True), encoding="utf-8"
    )
    monkeypatch.setenv("VIBECAD_HOME", str(home))
    monkeypatch.setenv("VIBECAD_FREECAD_ENV", str(legacy))
    status.write_external_runtime_receipt(legacy)
    before = _fingerprint(legacy)
    monkeypatch.delenv("VIBECAD_FREECAD_ENV")

    assert uninstall.uninstall_now()["ok"] is True

    assert _fingerprint(legacy) == before
    assert not (home / "runtime").exists()


def test_external_kind_legacy_is_ambiguous_to_uninstall_and_survives(monkeypatch, tmp_path):
    home = tmp_path / "VibeCAD"
    legacy = home / "mamba" / "envs" / "vibecad"
    legacy.mkdir(parents=True)
    (legacy / ".vibecad_ready").write_text(
        json.dumps(spec.expected_receipt(external=True), sort_keys=True), encoding="utf-8"
    )
    (legacy / "engine.bin").write_bytes(b"external legacy")
    monkeypatch.setenv("VIBECAD_HOME", str(home))
    before = _fingerprint(legacy)

    info = uninstall.uninstall_now()

    assert info["ok"] is True and info["already_clean"] is True
    assert str(legacy) in info["preserved_paths"]
    assert _fingerprint(legacy) == before


def test_partial_pending_removal_retains_marker_and_retries_without_touching_data(
    monkeypatch, tmp_path
):
    home = tmp_path / "VibeCAD"
    runtime = home / "runtime"
    data = home / "data"
    runtime.mkdir(parents=True)
    (runtime / "engine.bin").write_bytes(b"runtime")
    data.mkdir()
    (data / "project.fcstd").write_bytes(b"durable")
    monkeypatch.setenv("VIBECAD_HOME", str(home))
    before = _fingerprint(data)
    assert uninstall.request_uninstall()["marked"] is True
    real_delete = uninstall._delete_private_target
    monkeypatch.setattr(
        uninstall,
        "_delete_private_target",
        lambda parent, name, target: (_ for _ in ()).throw(OSError("simulated lock")),
    )

    assert uninstall.perform_pending_uninstall() is False
    assert uninstall.uninstall_marker().exists()
    assert runtime.exists()
    assert _fingerprint(data) == before

    monkeypatch.setattr(uninstall, "_delete_private_target", real_delete)
    assert uninstall.perform_pending_uninstall() is True
    assert not runtime.exists() and not uninstall.uninstall_marker().exists()
    assert _fingerprint(data) == before


def test_runtime_identity_replacement_between_plan_and_delete_fails_closed(monkeypatch, tmp_path):
    home = tmp_path / "VibeCAD"
    runtime = home / "runtime"
    runtime.mkdir(parents=True)
    (runtime / "old.bin").write_bytes(b"old")
    monkeypatch.setenv("VIBECAD_HOME", str(home))
    plan = uninstall._build_plan()
    target = next(item for item in plan.targets if item.path == runtime)
    parked = home / "parked-runtime"
    runtime.rename(parked)
    runtime.mkdir()
    replacement = runtime / "new.bin"
    replacement.write_bytes(b"replacement")

    try:
        uninstall._remove_target(target)
    except ValueError as exc:
        assert "identity" in str(exc)
    else:
        raise AssertionError("replacement target must not be removed")

    assert replacement.read_bytes() == b"replacement"
    assert (parked / "old.bin").read_bytes() == b"old"


@pytest.mark.parametrize("dir_fd_supported", [True, False])
def test_runtime_replacement_in_exact_check_to_park_window_is_restored(
    monkeypatch, tmp_path, dir_fd_supported
):
    home = tmp_path / "VibeCAD"
    runtime = home / "runtime"
    runtime.mkdir(parents=True)
    (runtime / "old.bin").write_bytes(b"old generation")
    monkeypatch.setenv("VIBECAD_HOME", str(home))
    if not dir_fd_supported:
        monkeypatch.setattr(uninstall, "_DIR_FD_SUPPORTED", False)
    target = next(item for item in uninstall._build_plan().targets if item.path == runtime)
    parked = uninstall._parked_path(runtime)
    old_generation = home / "published-old-generation"
    real_rename = uninstall.os.rename
    published = False

    def publish_replacement_before_park(source, destination, *, src_dir_fd=None, dst_dir_fd=None):
        nonlocal published
        if (
            not published
            and Path(source).name == runtime.name
            and Path(destination).name == parked.name
        ):
            published = True
            real_rename(runtime, old_generation)
            runtime.mkdir()
            (runtime / "new.bin").write_bytes(b"new generation")
        if src_dir_fd is None and dst_dir_fd is None:
            return real_rename(source, destination)
        return real_rename(
            source,
            destination,
            src_dir_fd=src_dir_fd,
            dst_dir_fd=dst_dir_fd,
        )

    monkeypatch.setattr(uninstall.os, "rename", publish_replacement_before_park)

    with pytest.raises(ValueError, match="identity"):
        uninstall._remove_target(target)

    assert published is True
    assert (runtime / "new.bin").read_bytes() == b"new generation"
    assert (old_generation / "old.bin").read_bytes() == b"old generation"
    assert not parked.exists()
    assert not uninstall.paths.removal_record().exists()


@pytest.mark.parametrize("kind", ["directory", "file"])
def test_parked_replacement_is_restored_before_any_destructive_delete(monkeypatch, tmp_path, kind):
    home = tmp_path / "VibeCAD"
    fixed = home / "runtime" if kind == "directory" else home / "bin" / "micromamba"
    fixed.parent.mkdir(parents=True)
    if kind == "directory":
        fixed.mkdir()
        (fixed / "old.bin").write_bytes(b"old generation")
    else:
        fixed.write_bytes(b"old generation")
    monkeypatch.setenv("VIBECAD_HOME", str(home))
    info = fixed.lstat()
    target = uninstall._Target(fixed, info.st_dev, info.st_ino, info.st_mode)
    parked = uninstall._parked_path(fixed)
    deleting = uninstall._deleting_path(fixed)
    displaced = fixed.with_name(f".{fixed.name}.displaced-old")
    real_rename = uninstall._rename_entry
    swapped = False

    def swap_parked_before_delete(parent, source, destination):
        nonlocal swapped
        if not swapped and source == parked.name and destination == deleting.name:
            swapped = True
            real_rename(parent, source, displaced.name)
            if kind == "directory":
                parked.mkdir()
                (parked / "replacement.bin").write_bytes(b"replacement")
            else:
                parked.write_bytes(b"replacement")
        real_rename(parent, source, destination)

    monkeypatch.setattr(uninstall, "_rename_entry", swap_parked_before_delete)

    with pytest.raises(ValueError, match="identity"):
        uninstall._remove_target(target)

    assert swapped is True
    replacement = parked / "replacement.bin" if kind == "directory" else parked
    old = displaced / "old.bin" if kind == "directory" else displaced
    assert replacement.read_bytes() == b"replacement"
    assert old.read_bytes() == b"old generation"
    assert not deleting.exists()


def test_restore_window_never_overwrites_new_fixed_generation(monkeypatch, tmp_path):
    home = tmp_path / "VibeCAD"
    runtime = home / "runtime"
    runtime.mkdir(parents=True)
    (runtime / "old.bin").write_bytes(b"old generation")
    monkeypatch.setenv("VIBECAD_HOME", str(home))
    target = next(item for item in uninstall._build_plan().targets if item.path == runtime)
    parked = uninstall._parked_path(runtime)
    real_rename_no_replace = uninstall._rename_no_replace
    published = False

    def fail_delete(parent, name, expected):
        raise OSError("simulated delete failure")

    def publish_before_restore(parent, source, destination):
        nonlocal published
        if not published and destination == runtime.name:
            published = True
            runtime.mkdir()
            (runtime / "new.bin").write_bytes(b"new generation")
        return real_rename_no_replace(parent, source, destination)

    monkeypatch.setattr(uninstall, "_delete_private_target", fail_delete)
    monkeypatch.setattr(uninstall, "_rename_no_replace", publish_before_restore)

    with pytest.raises(OSError, match="simulated delete failure"):
        uninstall._remove_target(target)

    assert published is True
    assert (runtime / "new.bin").read_bytes() == b"new generation"
    assert (parked / "old.bin").read_bytes() == b"old generation"
    assert uninstall.paths.removal_record().exists()


def test_restore_no_replace_never_overwrites_a_regular_generation(monkeypatch, tmp_path):
    parent_path = tmp_path / "parent"
    parent_path.mkdir()
    source = parent_path / "parked-micromamba"
    destination = parent_path / "micromamba"
    source.write_bytes(b"old")
    expected = source.lstat()
    real_rename_no_replace = uninstall._rename_no_replace
    published = False

    def publish_before_restore(parent, source_name, destination_name):
        nonlocal published
        published = True
        destination.write_bytes(b"new")
        return real_rename_no_replace(parent, source_name, destination_name)

    monkeypatch.setattr(uninstall, "_rename_no_replace", publish_before_restore)
    with uninstall._open_pinned_parent(parent_path) as parent:
        restored = uninstall._restore_moved_entry(
            parent,
            source.name,
            destination.name,
            expected,
        )

    assert published is True
    assert restored is False
    assert source.read_bytes() == b"old"
    assert destination.read_bytes() == b"new"


def test_runtime_is_atomically_parked_before_recursive_delete(monkeypatch, tmp_path):
    """A normally published new generation must never become the delete operand."""
    home = tmp_path / "VibeCAD"
    runtime = home / "runtime"
    runtime.mkdir(parents=True)
    (runtime / "old.bin").write_bytes(b"old generation")
    monkeypatch.setenv("VIBECAD_HOME", str(home))
    target = next(item for item in uninstall._build_plan().targets if item.path == runtime)
    real_delete = uninstall._delete_private_target
    published = False

    def publish_new_generation_then_delete(parent, name, expected):
        nonlocal published
        if not published:
            published = True
            runtime.mkdir()
            (runtime / "new.bin").write_bytes(b"new generation")
        real_delete(parent, name, expected)

    monkeypatch.setattr(uninstall, "_delete_private_target", publish_new_generation_then_delete)

    uninstall._remove_target(target)

    assert (runtime / "new.bin").read_bytes() == b"new generation"


def test_pending_uninstall_holds_home_lock_until_marker_is_cleared(monkeypatch, tmp_path):
    """A normal repair may publish only after the old pending marker is resolved."""
    home = tmp_path / "VibeCAD"
    runtime = home / "runtime"
    runtime.mkdir(parents=True)
    (runtime / "old.bin").write_bytes(b"old generation")
    monkeypatch.setenv("VIBECAD_HOME", str(home))
    assert uninstall.request_uninstall()["marked"] is True

    maintenance_lock = home / ".runtime-maintenance.lock"
    rescan_complete = threading.Event()
    first_lock_attempt_complete = threading.Event()
    published = threading.Event()
    marker_seen_at_publish: list[bool] = []
    real_build_plan = uninstall._build_plan
    plan_calls = 0

    def pause_after_remaining_rescan():
        nonlocal plan_calls
        result = real_build_plan()
        plan_calls += 1
        if plan_calls == 2:
            rescan_complete.set()
            assert first_lock_attempt_complete.wait(timeout=2)
        return result

    monkeypatch.setattr(uninstall, "_build_plan", pause_after_remaining_rescan)

    def normal_repair():
        assert rescan_complete.wait(timeout=2)
        lock = status.FileLock(maintenance_lock)
        acquired = lock.try_acquire()
        first_lock_attempt_complete.set()
        deadline = time.monotonic() + 2
        while not acquired and time.monotonic() < deadline:
            time.sleep(0.005)
            status._ensure_maintenance_write_root()
            acquired = lock.try_acquire()
        assert acquired
        try:
            marker_seen_at_publish.append(uninstall.uninstall_marker().exists())
            runtime.mkdir(parents=True)
            (runtime / "new.bin").write_bytes(b"new generation")
            published.set()
        finally:
            lock._force_remove()

    repair = threading.Thread(target=normal_repair)
    repair.start()
    try:
        assert uninstall.perform_pending_uninstall() is True
    finally:
        repair.join(timeout=3)

    assert not repair.is_alive()
    assert published.is_set()
    assert marker_seen_at_publish == [False]
    assert (runtime / "new.bin").read_bytes() == b"new generation"


def test_pending_retry_converges_from_identity_recorded_post_rename_crash(monkeypatch, tmp_path):
    home = tmp_path / "VibeCAD"
    runtime = home / "runtime"
    runtime.mkdir(parents=True)
    (runtime / "old.bin").write_bytes(b"old generation")
    monkeypatch.setenv("VIBECAD_HOME", str(home))
    assert uninstall.request_uninstall()["marked"] is True
    target = next(item for item in uninstall._build_plan().targets if item.path == runtime)
    parked = uninstall._parked_path(runtime)

    # Exact hard-crash state: durable identity was committed and the fixed target
    # was atomically parked, but recursive deletion never began.
    uninstall._write_removal_record(target)
    runtime.rename(parked)

    assert uninstall.perform_pending_uninstall() is True
    assert not parked.exists()
    assert not uninstall.paths.removal_record().exists()
    assert not uninstall.uninstall_marker().exists()
    assert not home.exists()


def test_recovery_restores_a_moved_replacement_without_old_identity(monkeypatch, tmp_path):
    home = tmp_path / "VibeCAD"
    runtime = home / "runtime"
    runtime.mkdir(parents=True)
    (runtime / "old.bin").write_bytes(b"old generation")
    monkeypatch.setenv("VIBECAD_HOME", str(home))
    target = next(item for item in uninstall._build_plan().targets if item.path == runtime)
    parked = uninstall._parked_path(runtime)
    displaced = home / "displaced-old-generation"
    uninstall._write_removal_record(target)
    runtime.rename(displaced)
    parked.mkdir()
    (parked / "new.bin").write_bytes(b"new generation")

    assert uninstall._recover_interrupted_removal() is True

    assert (runtime / "new.bin").read_bytes() == b"new generation"
    assert (displaced / "old.bin").read_bytes() == b"old generation"
    assert not parked.exists()
    assert not uninstall.paths.removal_record().exists()


def test_request_recovers_identity_recorded_direct_uninstall_crash(monkeypatch, tmp_path):
    home = tmp_path / "VibeCAD"
    runtime = home / "runtime"
    runtime.mkdir(parents=True)
    (runtime / "old.bin").write_bytes(b"old generation")
    monkeypatch.setenv("VIBECAD_HOME", str(home))
    target = next(item for item in uninstall._build_plan().targets if item.path == runtime)
    parked = uninstall._parked_path(runtime)
    uninstall._write_removal_record(target)
    runtime.rename(parked)

    result = uninstall.request_uninstall()

    assert result["ok"] is True and result["already_clean"] is True
    assert not parked.exists()
    assert not uninstall.paths.removal_record().exists()
    assert not home.exists()


def test_removal_record_rejects_bool_for_every_integer_evidence_field(monkeypatch, tmp_path):
    for field in ("schema", "device", "inode", "mode"):
        home = tmp_path / field
        home.mkdir()
        monkeypatch.setenv("VIBECAD_HOME", str(home))
        payload = {
            "schema": 1,
            "path": str(home / "runtime"),
            "device": 1,
            "inode": 2,
            "mode": stat.S_IFDIR | 0o700,
        }
        payload[field] = True
        record = uninstall.paths.removal_record()
        record.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")

        result = uninstall.request_uninstall()

        assert result["ok"] is False
        assert record.exists()


def test_unrecorded_reserved_park_is_never_guessed_away(monkeypatch, tmp_path):
    home = tmp_path / "VibeCAD"
    runtime = home / "runtime"
    runtime.mkdir(parents=True)
    (runtime / "old.bin").write_bytes(b"old generation")
    monkeypatch.setenv("VIBECAD_HOME", str(home))
    assert uninstall.request_uninstall()["marked"] is True
    unrelated = uninstall._parked_path(runtime)
    unrelated.mkdir()
    (unrelated / "user.bin").write_bytes(b"unrelated")

    assert uninstall.perform_pending_uninstall() is False
    assert (runtime / "old.bin").read_bytes() == b"old generation"
    assert (unrelated / "user.bin").read_bytes() == b"unrelated"
    assert uninstall.uninstall_marker().exists()


def test_nested_target_parent_is_synced_before_removal_record_is_cleared(monkeypatch, tmp_path):
    home = tmp_path / "VibeCAD"
    fixed = home / "mamba" / "envs" / "vibecad"
    fixed.mkdir(parents=True)
    (fixed / "old.bin").write_bytes(b"old generation")
    monkeypatch.setenv("VIBECAD_HOME", str(home))
    info = fixed.lstat()
    target = uninstall._Target(fixed, info.st_dev, info.st_ino, info.st_mode)
    parent_identity = (fixed.parent.stat().st_dev, fixed.parent.stat().st_ino)
    home_identity = (home.stat().st_dev, home.stat().st_ino)
    real_fsync = uninstall.os.fsync
    directory_syncs = []

    def record_directory_sync(fd):
        value = uninstall.os.fstat(fd)
        if stat.S_ISDIR(value.st_mode):
            identity = (value.st_dev, value.st_ino)
            if identity == parent_identity:
                directory_syncs.append("target-parent")
            elif identity == home_identity:
                directory_syncs.append("home")
        return real_fsync(fd)

    monkeypatch.setattr(uninstall.os, "fsync", record_directory_sync)

    uninstall._remove_target(target)

    assert directory_syncs[-2:] == ["target-parent", "home"]
    assert not fixed.exists()


def test_write_marker_is_idempotent_for_an_existing_regular_file(monkeypatch, tmp_path):
    home = tmp_path / "VibeCAD"
    home.mkdir()
    monkeypatch.setenv("VIBECAD_HOME", str(home))
    marker = uninstall.uninstall_marker()
    marker.write_bytes(b"existing marker")

    uninstall._write_marker()

    assert marker.read_bytes() == b"existing marker"


@pytest.mark.skipif(not hasattr(uninstall.os, "mkfifo"), reason="FIFO requires POSIX")
def test_write_marker_rejects_existing_fifo_without_opening_it(monkeypatch, tmp_path):
    home = tmp_path / "VibeCAD"
    home.mkdir()
    monkeypatch.setenv("VIBECAD_HOME", str(home))
    marker = uninstall.uninstall_marker()
    uninstall.os.mkfifo(marker)

    with pytest.raises(ValueError, match="普通文件"):
        uninstall._write_marker()

    assert stat.S_ISFIFO(marker.lstat().st_mode)


@pytest.mark.skipif(not hasattr(uninstall.os, "mkfifo"), reason="FIFO requires POSIX")
def test_pending_uninstall_rejects_fifo_marker_without_removing_runtime(
    monkeypatch,
    tmp_path,
):
    home = tmp_path / "VibeCAD"
    runtime = home / "runtime"
    runtime.mkdir(parents=True)
    payload = runtime / "engine.bin"
    payload.write_bytes(b"runtime")
    monkeypatch.setenv("VIBECAD_HOME", str(home))
    marker = uninstall.uninstall_marker()
    uninstall.os.mkfifo(marker)

    assert uninstall.perform_pending_uninstall() is False
    assert payload.read_bytes() == b"runtime"
    assert stat.S_ISFIFO(marker.lstat().st_mode)
