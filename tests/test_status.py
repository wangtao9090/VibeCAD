import hashlib
import json
import os
import stat
import sys
import threading
from pathlib import Path

import pytest

from vibecad.runtime import spec, status


def test_fresh_home_maintenance_root_creation_is_concurrency_safe(monkeypatch, tmp_path):
    home = tmp_path / "fresh-home"
    monkeypatch.setenv("VIBECAD_HOME", str(home))
    original_lexists = status.os.path.lexists
    old_check_barrier = threading.Barrier(2)

    def synchronize_the_old_check(path):
        exists = original_lexists(path)
        if Path(path) == home and not exists:
            old_check_barrier.wait(timeout=2)
        return exists

    monkeypatch.setattr(status.os.path, "lexists", synchronize_the_old_check)
    errors: list[BaseException] = []

    def create() -> None:
        try:
            status._ensure_maintenance_write_root()
        except BaseException as error:
            errors.append(error)

    workers = [threading.Thread(target=create) for _ in range(2)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=3)

    assert all(not worker.is_alive() for worker in workers)
    assert errors == []
    assert home.is_dir() and not home.is_symlink()


def test_maintenance_root_never_follows_a_symlink_ancestor(monkeypatch, tmp_path):
    durable = tmp_path / "durable"
    durable.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(durable, target_is_directory=True)
    monkeypatch.setenv("VIBECAD_HOME", str(alias / "vibecad-home"))

    with pytest.raises(ValueError, match="unavailable"):
        status.write_status(status.RuntimeStatus(message="unsafe"))

    assert tuple(durable.iterdir()) == ()


def test_external_receipt_rejects_prefix_inside_replaceable_runtime(monkeypatch, tmp_path):
    home = tmp_path / "home"
    prefix = home / "runtime" / "external-env"
    (prefix / "bin").mkdir(parents=True)
    (prefix / "bin" / "python").write_bytes(b"python")
    monkeypatch.setenv("VIBECAD_HOME", str(home))

    with pytest.raises(ValueError, match="overlaps"):
        status.write_external_runtime_receipt(prefix)

    assert not status.paths.external_runtime_receipt().exists()


def test_status_roundtrip(monkeypatch, tmp_path):
    monkeypatch.setenv("VIBECAD_HOME", str(tmp_path))
    s = status.RuntimeStatus(phase=status.Phase.CREATING_ENV, percent=20.0, message="建环境")
    status.write_status(s)
    got = status.read_status()
    assert got.phase is status.Phase.CREATING_ENV and got.percent == 20.0
    assert status.read_status().to_dict()["message"] == "建环境"
    assert status.paths.status_file() == tmp_path / "runtime" / "status.json"


def test_read_status_default_when_absent(monkeypatch, tmp_path):
    monkeypatch.setenv("VIBECAD_HOME", str(tmp_path))
    assert status.read_status().phase is status.Phase.NOT_STARTED


def test_read_status_rejects_fifo_without_blocking(monkeypatch, tmp_path):
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    os.mkfifo(runtime / "status.json", 0o600)
    monkeypatch.setenv("VIBECAD_HOME", str(tmp_path))

    assert status.read_status() == status.RuntimeStatus()


def _managed_paths(monkeypatch, tmp_path):
    monkeypatch.delenv("VIBECAD_FREECAD_ENV", raising=False)
    monkeypatch.setenv("VIBECAD_HOME", str(tmp_path))
    python = status.paths.active_runtime_python()
    python.parent.mkdir(parents=True, exist_ok=True)
    python.touch()
    return status.paths.ready_sentinel(), python


def _canonical_public_surface_sha256() -> str:
    from collections.abc import Mapping

    from vibecad.application.public_surface import public_tool_specs

    def thaw(value):
        if value is None or type(value) in {str, int, float, bool}:
            return value
        if type(value) in {tuple, list}:
            return [thaw(item) for item in value]
        if isinstance(value, Mapping):
            return {key: thaw(value[key]) for key in sorted(value)}
        raise TypeError(f"unsupported public surface value: {type(value)!r}")

    projection = [
        {
            "name": item.name,
            "description": item.description,
            "inputSchema": thaw(item.input_schema),
            "outputSchema": thaw(item.output_schema),
            "annotations": {
                "readOnlyHint": item.annotations.read_only,
                "destructiveHint": item.annotations.destructive,
                "idempotentHint": item.annotations.idempotent,
                "openWorldHint": item.annotations.open_world,
            },
        }
        for item in public_tool_specs()
    ]
    raw = json.dumps(
        projection,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def test_expected_receipts_bind_private_package_epoch_sdk_and_public_surface():
    common = {
        "schema": spec.RECEIPT_SCHEMA,
        "vibecad_version": spec.VIBECAD_VERSION,
        "server_package_epoch": spec.SERVER_PACKAGE_EPOCH,
        "mcp_version": "1.27.2",
        "public_surface_sha256": spec.PUBLIC_SURFACE_SHA256,
    }

    assert spec.VIBECAD_VERSION == "0.5.0"
    assert type(spec.SERVER_PACKAGE_EPOCH) is int and spec.SERVER_PACKAGE_EPOCH == 4
    assert spec.MCP_VERSION == "1.27.2"
    assert spec.PUBLIC_SURFACE_SHA256 == _canonical_public_surface_sha256()
    assert spec.expected_receipt() == {
        **common,
        "runtime_kind": spec.MANAGED_KIND,
        "python_pin": spec.PYTHON_PIN,
        "freecad_pin": spec.FREECAD_PIN,
    }
    assert spec.expected_receipt(external=True) == {
        **common,
        "runtime_kind": spec.EXTERNAL_KIND,
    }


def test_runtime_ready_requires_current_json_receipt_and_python(monkeypatch, tmp_path):
    sentinel, python = _managed_paths(monkeypatch, tmp_path)
    assert status.runtime_ready() is False

    status.write_runtime_receipt()
    assert status.read_runtime_receipt() == spec.expected_receipt()
    assert status.runtime_receipt_state() is status.ReceiptState.CURRENT
    assert status.runtime_ready() is True

    python.unlink()
    assert status.runtime_ready() is False
    assert sentinel.exists()  # Python 缺失不能因 receipt 仍在而误判就绪


def test_legacy_receipt_requires_server_sync(monkeypatch, tmp_path):
    sentinel, _ = _managed_paths(monkeypatch, tmp_path)
    sentinel.write_text(spec.FREECAD_PIN, encoding="utf-8")
    assert status.runtime_receipt_state() is status.ReceiptState.LEGACY
    assert status.read_runtime_receipt() is None
    assert status.runtime_ready() is False


def test_server_version_mismatch_is_not_ready(monkeypatch, tmp_path):
    sentinel, _ = _managed_paths(monkeypatch, tmp_path)
    receipt = spec.expected_receipt()
    receipt["vibecad_version"] = "0.3.0"
    sentinel.write_text(json.dumps(receipt), encoding="utf-8")
    assert status.runtime_receipt_state() is status.ReceiptState.SERVER_MISMATCH
    assert status.runtime_ready() is False


def test_same_version_pre_epoch_managed_receipt_requires_pip_only_server_sync(
    monkeypatch,
    tmp_path,
):
    sentinel, _ = _managed_paths(monkeypatch, tmp_path)
    old_receipt = {
        "schema": spec.RECEIPT_SCHEMA,
        "runtime_kind": spec.MANAGED_KIND,
        "vibecad_version": spec.VIBECAD_VERSION,
        "python_pin": spec.PYTHON_PIN,
        "freecad_pin": spec.FREECAD_PIN,
    }
    sentinel.write_text(json.dumps(old_receipt, sort_keys=True), encoding="utf-8")

    assert status.runtime_receipt_state() is status.ReceiptState.SERVER_MISMATCH
    assert status.runtime_recovery_kind() is status.RecoveryKind.UPGRADE_REQUIRED
    assert status.runtime_ready() is False


def test_same_version_previous_positive_epoch_requires_pip_only_server_sync(
    monkeypatch,
    tmp_path,
):
    sentinel, _ = _managed_paths(monkeypatch, tmp_path)
    old_receipt = {
        **spec.expected_receipt(),
        "server_package_epoch": spec.SERVER_PACKAGE_EPOCH - 1,
    }
    sentinel.write_text(json.dumps(old_receipt, sort_keys=True), encoding="utf-8")

    assert status.runtime_receipt_state() is status.ReceiptState.SERVER_MISMATCH
    assert status.runtime_recovery_kind() is status.RecoveryKind.UPGRADE_REQUIRED
    assert status.runtime_ready() is False


@pytest.mark.parametrize("field", ("schema", "server_package_epoch"))
def test_managed_receipt_rejects_boolean_for_integer_identity(
    monkeypatch,
    tmp_path,
    field,
):
    sentinel, _ = _managed_paths(monkeypatch, tmp_path)
    receipt = spec.expected_receipt()
    receipt[field] = True
    sentinel.write_text(json.dumps(receipt, sort_keys=True), encoding="utf-8")

    assert status.runtime_receipt_state() is status.ReceiptState.INCOMPATIBLE
    assert status.runtime_recovery_kind() is status.RecoveryKind.REPAIR_REQUIRED
    assert status.runtime_ready() is False


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("server_package_epoch", spec.SERVER_PACKAGE_EPOCH + 1),
        ("mcp_version", "1.28.0"),
        ("public_surface_sha256", "b" * 64),
    ),
)
def test_well_formed_managed_server_identity_drift_requires_pip_only_sync(
    monkeypatch,
    tmp_path,
    field,
    value,
):
    sentinel, _ = _managed_paths(monkeypatch, tmp_path)
    receipt = spec.expected_receipt()
    receipt[field] = value
    sentinel.write_text(json.dumps(receipt, sort_keys=True), encoding="utf-8")

    assert status.runtime_receipt_state() is status.ReceiptState.SERVER_MISMATCH
    assert status.runtime_recovery_kind() is status.RecoveryKind.UPGRADE_REQUIRED


def test_fixed_legacy_managed_ownership_accepts_well_formed_future_server_identity(
    monkeypatch,
    tmp_path,
):
    home = tmp_path / "home"
    monkeypatch.setenv("VIBECAD_HOME", str(home))
    legacy = status.paths.legacy_env_prefix()
    legacy.mkdir(parents=True)
    receipt = spec.expected_receipt()
    receipt["server_package_epoch"] += 1
    (legacy / ".vibecad_ready").write_text(
        json.dumps(receipt, sort_keys=True),
        encoding="utf-8",
    )

    assert status.managed_legacy_receipt(legacy) == receipt


def test_corrupt_or_engine_mismatch_receipt_is_incompatible(monkeypatch, tmp_path):
    sentinel, _ = _managed_paths(monkeypatch, tmp_path)
    sentinel.write_text("{broken", encoding="utf-8")
    assert status.runtime_receipt_state() is status.ReceiptState.INCOMPATIBLE
    assert status.runtime_ready() is False

    receipt = spec.expected_receipt()
    receipt["freecad_pin"] = "freecad=9.9"
    sentinel.write_text(json.dumps(receipt), encoding="utf-8")
    assert status.runtime_receipt_state() is status.ReceiptState.INCOMPATIBLE
    assert status.runtime_ready() is False


def test_write_runtime_receipt_uses_atomic_replace(monkeypatch, tmp_path):
    sentinel, _ = _managed_paths(monkeypatch, tmp_path)
    replaced = []
    real_replace = status.os.replace

    def record_replace(src, dst, *, src_dir_fd=None, dst_dir_fd=None):
        replaced.append((src, dst, src_dir_fd, dst_dir_fd))
        real_replace(
            src,
            dst,
            src_dir_fd=src_dir_fd,
            dst_dir_fd=dst_dir_fd,
        )

    monkeypatch.setattr(status.os, "replace", record_replace)
    status.write_runtime_receipt()

    assert replaced and replaced[0][1] == sentinel.name
    assert replaced[0][0] != sentinel.name
    assert replaced[0][2] == replaced[0][3]
    assert json.loads(sentinel.read_text(encoding="utf-8")) == spec.expected_receipt()
    assert not (sentinel.parent / replaced[0][0]).exists()


@pytest.mark.parametrize("writer", ["status", "managed", "external"])
def test_runtime_writes_never_follow_parent_replacement_into_data(
    writer,
    monkeypatch,
    tmp_path,
):
    home = tmp_path / "VibeCAD"
    runtime = home / "runtime"
    data = home / "data"
    env = runtime / "mamba" / "envs" / "vibecad"
    (env / "bin").mkdir(parents=True)
    (env / "bin" / "python").write_bytes(b"python")
    data.mkdir()
    durable = data / "HEAD"
    durable.write_bytes(b"durable")
    external = tmp_path / "external"
    (external / "bin").mkdir(parents=True)
    (external / "bin" / "python").write_bytes(b"python")
    monkeypatch.setenv("VIBECAD_HOME", str(home))
    monkeypatch.setenv("VIBECAD_FREECAD_ENV", str(external))
    original_atomic_write = status._atomic_write
    swapped = False

    def write_after_swap(path, raw, *, pinned_parent=None):
        nonlocal swapped
        if not swapped:
            swapped = True
            runtime.rename(home / "detached-runtime")
            runtime.symlink_to(data, target_is_directory=True)
        return original_atomic_write(path, raw, pinned_parent=pinned_parent)

    monkeypatch.setattr(status, "_atomic_write", write_after_swap)
    with pytest.raises(ValueError, match="identity changed"):
        if writer == "status":
            status.write_status(status.RuntimeStatus(message="unsafe"))
        elif writer == "managed":
            monkeypatch.delenv("VIBECAD_FREECAD_ENV")
            status.write_managed_runtime_receipt(status.paths.env_prefix())
        else:
            status.write_external_runtime_receipt(external)

    assert durable.read_bytes() == b"durable"
    assert tuple(path.name for path in data.iterdir()) == ("HEAD",)


def test_current_managed_receipt_never_creates_a_missing_env(monkeypatch, tmp_path):
    monkeypatch.setenv("VIBECAD_HOME", str(tmp_path))
    prefix = status.paths.env_prefix()

    with pytest.raises(ValueError, match="unavailable"):
        status.write_managed_runtime_receipt(prefix)

    assert not prefix.exists()


def test_verified_generation_evidence_rejects_replaced_interpreter(monkeypatch, tmp_path):
    sentinel, python = _managed_paths(monkeypatch, tmp_path)
    python.write_bytes(b"verified")
    evidence = status.capture_runtime_generation_evidence(status.paths.env_prefix())
    replacement = python.with_name("replacement")
    replacement.write_bytes(b"different generation")
    replacement.replace(python)

    with pytest.raises(ValueError, match="generation|identity"):
        status.write_managed_runtime_receipt(
            status.paths.env_prefix(),
            evidence=evidence,
        )

    assert not sentinel.exists()


def test_receipt_postcheck_revokes_its_publication_after_interpreter_swap(
    monkeypatch,
    tmp_path,
):
    sentinel, python = _managed_paths(monkeypatch, tmp_path)
    python.write_bytes(b"verified")
    evidence = status.capture_runtime_generation_evidence(status.paths.env_prefix())
    real_atomic_write = status._atomic_write

    def publish_then_swap(path, raw, *, pinned_parent=None):
        published = real_atomic_write(path, raw, pinned_parent=pinned_parent)
        replacement = python.with_name("postcheck-replacement")
        replacement.write_bytes(b"different generation")
        replacement.replace(python)
        return published

    monkeypatch.setattr(status, "_atomic_write", publish_then_swap)
    with pytest.raises(ValueError, match="generation|identity"):
        status.write_managed_runtime_receipt(
            status.paths.env_prefix(),
            evidence=evidence,
        )

    assert not sentinel.exists()
    assert status.runtime_ready() is False


def test_current_managed_readiness_rejects_nested_prefix_alias(monkeypatch, tmp_path):
    _sentinel, python = _managed_paths(monkeypatch, tmp_path)
    python.write_bytes(b"python")
    status.write_runtime_receipt()
    mamba = status.paths.mamba_root_prefix()
    parked = tmp_path / "parked-mamba"
    mamba.rename(parked)
    mamba.symlink_to(parked, target_is_directory=True)

    assert status.read_runtime_receipt() is None
    assert status.runtime_ready() is False


def test_safe_install_log_append_rejects_link_into_data(monkeypatch, tmp_path):
    runtime = tmp_path / "runtime"
    data = tmp_path / "data"
    runtime.mkdir()
    data.mkdir()
    durable = data / "HEAD"
    durable.write_bytes(b"durable")
    (runtime / "install.log").symlink_to(durable)
    monkeypatch.setenv("VIBECAD_HOME", str(tmp_path))

    with pytest.raises(ValueError, match="log"):
        status.append_install_log("must not escape\n")

    assert durable.read_bytes() == b"durable"


def test_safe_install_log_append_is_bounded_and_appends(monkeypatch, tmp_path):
    monkeypatch.setenv("VIBECAD_HOME", str(tmp_path))
    status.append_install_log("first\n")
    status.append_install_log("second\n")
    assert status.paths.install_log().read_text(encoding="utf-8") == "first\nsecond\n"

    with pytest.raises(ValueError, match="too large"):
        status.append_install_log("x" * (status._MAX_LOG_APPEND_BYTES + 1))


def test_receipt_reader_rejects_fifo_without_blocking(monkeypatch, tmp_path):
    home = tmp_path / "VibeCAD"
    legacy = home / "mamba" / "envs" / "vibecad"
    legacy.mkdir(parents=True)
    receipt = legacy / ".vibecad_ready"
    os.mkfifo(receipt, 0o600)
    monkeypatch.setenv("VIBECAD_HOME", str(home))

    assert status.read_prefix_receipt(legacy) is None
    assert status.managed_legacy_receipt(legacy) is None


def test_probe_is_isolated_and_never_writes_external_bytecode(
    monkeypatch,
    tmp_path,
):
    module = tmp_path / "external_runtime_probe.py"
    module.write_text("VALUE = 1\n", encoding="utf-8")
    monkeypatch.setenv("PYTHONPATH", str(tmp_path))

    assert status._probe(
        Path(sys.executable),
        f"import sys; sys.path.insert(0, {str(tmp_path)!r}); import external_runtime_probe",
    )
    assert not (tmp_path / "__pycache__").exists()
    assert status._probe(Path(sys.executable), "import external_runtime_probe") is False


_FREECAD_PROCESS_ENV_KEYS = frozenset(
    {"FREECAD_USER_HOME", "FREECAD_USER_DATA", "FREECAD_USER_TEMP"}
)


def _expected_freecad_process_environment(home: Path) -> dict[str, str]:
    root = home / "runtime" / "freecad-user"
    return {
        "FREECAD_USER_HOME": str(root / "home"),
        "FREECAD_USER_DATA": str(root / "data"),
        "FREECAD_USER_TEMP": str(root / "temp"),
    }


def test_freecad_process_environment_is_private_replaceable_and_exact(
    monkeypatch,
    tmp_path,
):
    home = tmp_path / "VibeCAD"
    legacy = home / "mamba" / "envs" / "vibecad"
    legacy.mkdir(parents=True)
    legacy_marker = legacy / "engine.bin"
    legacy_marker.write_bytes(b"external-engine")
    monkeypatch.setenv("VIBECAD_HOME", str(home))
    base = {
        "KEEP": "yes",
        "FREECAD_USER_HOME": "attacker-home",
        "FREECAD_USER_DATA": "attacker-data",
        "FREECAD_USER_TEMP": "attacker-temp",
    }
    base_before = dict(base)
    process_before = {key: os.environ.get(key) for key in _FREECAD_PROCESS_ENV_KEYS}
    expected = _expected_freecad_process_environment(home)

    environment = status.freecad_process_environment(base)

    assert base == base_before
    assert environment == {"KEEP": "yes", **expected}
    assert status.freecad_process_environment() == expected
    assert {key: os.environ.get(key) for key in _FREECAD_PROCESS_ENV_KEYS} == process_before
    assert legacy_marker.read_bytes() == b"external-engine"
    assert not (home / "data").exists()
    for directory in [home / "runtime" / "freecad-user", *map(Path, expected.values())]:
        info = directory.lstat()
        assert stat.S_ISDIR(info.st_mode)
        assert stat.S_IMODE(info.st_mode) & 0o077 == 0


def test_freecad_process_environment_rejects_alias_without_touching_target(
    monkeypatch,
    tmp_path,
):
    home = tmp_path / "VibeCAD"
    runtime = home / "runtime"
    runtime.mkdir(parents=True, mode=0o700)
    outside = tmp_path / "outside"
    outside.mkdir()
    (runtime / "freecad-user").symlink_to(outside, target_is_directory=True)
    monkeypatch.setenv("VIBECAD_HOME", str(home))

    with pytest.raises(ValueError, match="FreeCAD process directory is unavailable"):
        status.freecad_process_environment()

    assert tuple(outside.iterdir()) == ()


@pytest.mark.parametrize("external_at", ["home", "runtime"])
def test_freecad_process_environment_rejects_external_runtime_overlap_before_write(
    monkeypatch,
    tmp_path,
    external_at,
):
    home = tmp_path / "VibeCAD"
    external = home if external_at == "home" else home / "runtime"
    external.mkdir(parents=True, mode=0o700)
    marker = external / "external-engine.bin"
    marker.write_bytes(b"external-engine")
    monkeypatch.setenv("VIBECAD_HOME", str(home))
    monkeypatch.setenv("VIBECAD_FREECAD_ENV", str(external))

    with pytest.raises(ValueError, match="FreeCAD process directory is unavailable"):
        status.freecad_process_environment()

    assert marker.read_bytes() == b"external-engine"
    assert not (home / "runtime" / "freecad-user").exists()


def test_freecad_process_environment_rejects_bound_external_overlap_before_write(
    monkeypatch,
    tmp_path,
):
    home = tmp_path / "VibeCAD"
    runtime = home / "runtime"
    runtime.mkdir(parents=True, mode=0o700)
    info = home.stat()
    receipt = runtime / "external-runtime.json"
    receipt.write_text(
        json.dumps(
            {
                "prefix": str(home),
                "prefix_device": info.st_dev,
                "prefix_inode": info.st_ino,
            }
        ),
        encoding="utf-8",
    )
    receipt.chmod(0o600)
    before = receipt.read_bytes()
    monkeypatch.setenv("VIBECAD_HOME", str(home))
    monkeypatch.delenv("VIBECAD_FREECAD_ENV", raising=False)

    with pytest.raises(ValueError, match="FreeCAD process directory is unavailable"):
        status.freecad_process_environment()

    assert receipt.read_bytes() == before
    assert not (runtime / "freecad-user").exists()


def test_freecad_process_environment_rejects_attacker_writable_ancestor(
    monkeypatch,
    tmp_path,
):
    public_parent = tmp_path / "public"
    public_parent.mkdir(mode=0o700)
    public_parent.chmod(0o777)
    home = public_parent / "VibeCAD"
    monkeypatch.setenv("VIBECAD_HOME", str(home))

    with pytest.raises(ValueError, match="FreeCAD process directory is unavailable"):
        status.freecad_process_environment()

    assert not (home / "runtime" / "freecad-user").exists()


def test_freecad_windows_fallback_does_not_apply_posix_mode_bits(
    monkeypatch,
    tmp_path,
):
    directory = tmp_path / "private"
    directory.mkdir(mode=0o700)
    monkeypatch.setattr(status.sys, "platform", "win32")
    monkeypatch.setattr(status.stat, "S_IMODE", lambda _mode: 0o777)

    assert status._fallback_private_directory(directory) == directory


def test_freecad_process_environment_detects_runtime_root_replacement(
    monkeypatch,
    tmp_path,
):
    home = tmp_path / "VibeCAD"
    runtime = home / "runtime"
    runtime.mkdir(parents=True, mode=0o700)
    data = home / "data"
    data.mkdir()
    durable = data / "HEAD"
    durable.write_bytes(b"durable")
    monkeypatch.setenv("VIBECAD_HOME", str(home))
    original = status._ensure_private_child_directory
    swapped = False

    def ensure_then_swap(parent, name):
        nonlocal swapped
        pinned = original(parent, name)
        if name == "freecad-user" and not swapped:
            swapped = True
            runtime.rename(home / "detached-runtime")
            runtime.symlink_to(data, target_is_directory=True)
        return pinned

    monkeypatch.setattr(status, "_ensure_private_child_directory", ensure_then_swap)

    with pytest.raises(ValueError, match="FreeCAD process directory is unavailable"):
        status.freecad_process_environment()

    assert durable.read_bytes() == b"durable"
    assert tuple(path.name for path in data.iterdir()) == ("HEAD",)


def test_freecad_process_environment_rejects_wrong_owner(monkeypatch, tmp_path):
    home = tmp_path / "VibeCAD"
    (home / "runtime").mkdir(parents=True, mode=0o700)
    monkeypatch.setenv("VIBECAD_HOME", str(home))
    current_uid = os.geteuid()
    monkeypatch.setattr(status.os, "geteuid", lambda: current_uid + 1)

    with pytest.raises(ValueError, match="FreeCAD process directory is unavailable"):
        status.freecad_process_environment()

    assert not (home / "runtime" / "freecad-user").exists()


def test_freecad_process_environment_creation_failure_does_not_reflect_path(
    monkeypatch,
    tmp_path,
):
    secret = "must-not-reflect-this-path"
    home = tmp_path / secret
    (home / "runtime").mkdir(parents=True, mode=0o700)
    monkeypatch.setenv("VIBECAD_HOME", str(home))
    original_mkdir = status.os.mkdir

    def deny_freecad_directory(path, mode=0o777, *, dir_fd=None):
        if path == "freecad-user":
            raise PermissionError(f"denied {home}")
        return original_mkdir(path, mode, dir_fd=dir_fd)

    monkeypatch.setattr(status.os, "mkdir", deny_freecad_directory)
    monkeypatch.setattr(status, "_secure_dir_fd_available", lambda: True)

    with pytest.raises(ValueError) as caught:
        status.freecad_process_environment({"FREECAD_USER_HOME": "also-must-not-be-reflected"})

    assert str(caught.value) == "FreeCAD process directory is unavailable"
    assert secret not in str(caught.value)
    assert "also-must-not-be-reflected" not in str(caught.value)


def test_probe_supplies_freecad_environment_when_passwd_lookup_is_unavailable(
    monkeypatch,
    tmp_path,
):
    home = tmp_path / "VibeCAD"
    monkeypatch.setenv("VIBECAD_HOME", str(home))
    for key in _FREECAD_PROCESS_ENV_KEYS:
        monkeypatch.setenv(key, f"poison-{key}")
    expected = _expected_freecad_process_environment(home)
    captured = {}

    class P:
        returncode = 0

    def child_with_no_passwd(_command, **kwargs):
        captured.update(kwargs["env"])
        # Model the FreeCAD failure observed on a host where getpwuid_r cannot
        # resolve the launching UID: only explicit existing custom directories
        # let the child initialize.
        if {key: kwargs["env"].get(key) for key in expected} != expected:
            return type("Failed", (), {"returncode": 1})()
        return P()

    monkeypatch.setattr(status.subprocess, "run", child_with_no_passwd)

    assert status._probe(Path(sys.executable), "pass") is True
    assert {key: captured[key] for key in expected} == expected
    assert captured["PYTHONDONTWRITEBYTECODE"] == "1"


@pytest.mark.parametrize("generation", [False, True])
def test_probe_fails_closed_when_freecad_environment_is_unavailable(
    monkeypatch,
    tmp_path,
    generation,
):
    home = tmp_path / "VibeCAD"
    monkeypatch.setenv("VIBECAD_HOME", str(home))

    def unavailable(_base=None):
        raise ValueError("FreeCAD process directory is unavailable")

    monkeypatch.setattr(status, "freecad_process_environment", unavailable, raising=False)
    monkeypatch.setattr(
        status.subprocess,
        "run",
        lambda *args, **kwargs: pytest.fail("probe must not spawn without a safe environment"),
    )
    monkeypatch.setattr(
        status,
        "_spawn_probe_process",
        lambda *args, **kwargs: pytest.fail("generation probe must not spawn unsafely"),
    )

    if generation:
        prefix = tmp_path / "external"
        python = status.paths.env_python_for(prefix)
        python.parent.mkdir(parents=True)
        python.write_bytes(b"python")
        evidence = status.capture_runtime_generation_evidence(prefix)
        assert status.verify_runtime_generation(evidence) is False
    else:
        assert status._probe(Path(sys.executable), "pass") is False


def test_generation_probe_spawn_uses_clean_helper_without_preexec(monkeypatch, tmp_path):
    home = tmp_path / "VibeCAD"
    monkeypatch.setenv("VIBECAD_HOME", str(home))
    prefix = tmp_path / "external"
    python = status.paths.env_python_for(prefix)
    python.parent.mkdir(parents=True)
    python.write_bytes(b"python")
    evidence = status.capture_runtime_generation_evidence(prefix)
    captured = {}

    class P:
        returncode = 0

    def fake_spawn(command, **kwargs):
        captured["command"] = command
        captured["kwargs"] = kwargs
        return P()

    monkeypatch.setattr(status, "_spawn_probe_process", fake_spawn)

    assert status.verify_runtime_generation(evidence) is True
    command = captured["command"]
    options = captured["kwargs"]
    assert "preexec_fn" not in options
    assert len(options["pass_fds"]) == 1
    assert command[:4] == [sys.executable, "-I", "-B", "-c"]
    assert command[4] == status._FD_EXEC_HELPER
    assert command[5] == str(options["pass_fds"][0])
    assert command[6:] == [f"./{python.name}", "-I", "-B", "-c", status._VERIFY_SNIPPET]
    assert {
        key: options["env"][key] for key in _FREECAD_PROCESS_ENV_KEYS
    } == _expected_freecad_process_environment(home)


def test_health_snippet_has_win_dll_prep():
    # M4: -c 片段在 import 前注入 PATH 兜底
    assert "Library" in status._HEALTH_SNIPPET and "import FreeCAD" in status._HEALTH_SNIPPET


def test_verify_snippet_requires_exact_vibecad_version():
    assert "vibecad.__version__" in status._VERIFY_SNIPPET
    assert spec.VIBECAD_VERSION in status._VERIFY_SNIPPET
    assert "raise RuntimeError" in status._VERIFY_SNIPPET
    assert "assert vibecad.__version__" not in status._VERIFY_SNIPPET


def test_verify_snippet_independently_checks_epoch_sdk_and_recomputed_surface():
    snippet = status._VERIFY_SNIPPET

    assert "SERVER_PACKAGE_EPOCH" in snippet
    assert repr(spec.SERVER_PACKAGE_EPOCH) in snippet
    assert "import mcp" in snippet
    assert "metadata.version" in snippet
    assert repr(spec.MCP_VERSION) in snippet
    assert "public_tool_specs" in snippet
    assert "hashlib.sha256" in snippet
    assert repr(spec.PUBLIC_SURFACE_SHA256) in snippet
    assert all(
        name in snippet
        for name in (
            "inputSchema",
            "outputSchema",
            "description",
            "readOnlyHint",
            "destructiveHint",
            "idempotentHint",
            "openWorldHint",
        )
    )


def test_server_verify_snippet_recomputes_the_live_public_surface():
    scope = {}

    exec(status._SERVER_SNIPPET, scope)

    assert scope["_surface_digest"] == spec.PUBLIC_SURFACE_SHA256
    assert scope["_surface_digest"] == _canonical_public_surface_sha256()


@pytest.mark.parametrize("drift", ("epoch", "mcp", "surface"))
def test_server_verify_snippet_rejects_each_independent_identity_drift(
    monkeypatch,
    drift,
):
    if drift == "epoch":
        monkeypatch.setattr(spec, "SERVER_PACKAGE_EPOCH", spec.SERVER_PACKAGE_EPOCH + 1)
    elif drift == "mcp":
        import importlib.metadata

        monkeypatch.setattr(importlib.metadata, "version", lambda name: "0.0.0")
    else:
        from vibecad.application import public_surface

        monkeypatch.setattr(public_surface, "public_tool_specs", lambda: ())

    with pytest.raises(RuntimeError):
        exec(status._SERVER_SNIPPET, {})


def test_engine_and_verify_snippets_enforce_exact_pins_without_assert():
    assert repr(spec.PYTHON_VERSION) in status._ENGINE_SNIPPET
    assert repr(spec.FREECAD_VERSION) in status._ENGINE_SNIPPET
    assert "sys.version_info[:2]" in status._VERIFY_SNIPPET
    assert "FreeCAD.Version()[:3]" in status._VERIFY_SNIPPET
    assert "raise RuntimeError" in status._ENGINE_SNIPPET
    assert "assert " not in status._ENGINE_SNIPPET


def test_runtime_recovery_kind_is_conservative(monkeypatch, tmp_path):
    sentinel, python = _managed_paths(monkeypatch, tmp_path)

    status.write_runtime_receipt()
    assert status.runtime_recovery_kind() is status.RecoveryKind.READY

    receipt = spec.expected_receipt()
    receipt["vibecad_version"] = "0.3.0"
    sentinel.write_text(json.dumps(receipt), encoding="utf-8")
    assert status.runtime_recovery_kind() is status.RecoveryKind.UPGRADE_REQUIRED

    sentinel.write_text("{broken", encoding="utf-8")
    assert status.runtime_recovery_kind() is status.RecoveryKind.REPAIR_REQUIRED

    sentinel.unlink()
    assert status.runtime_recovery_kind() is status.RecoveryKind.REPAIR_REQUIRED

    status.write_runtime_receipt()
    python.unlink()
    assert status.runtime_recovery_kind() is status.RecoveryKind.REPAIR_REQUIRED


@pytest.mark.parametrize("legacy", [False, True])
def test_external_runtime_never_promises_automatic_server_upgrade(
    monkeypatch,
    tmp_path,
    legacy,
):
    monkeypatch.setenv("VIBECAD_HOME", str(tmp_path / "home"))
    override = tmp_path / "external"
    python = status.paths.env_python_for(override)
    python.parent.mkdir(parents=True)
    python.touch()
    monkeypatch.setenv("VIBECAD_FREECAD_ENV", str(override))
    sentinel = status.paths.ready_sentinel()
    sentinel.parent.mkdir(parents=True)
    if legacy:
        sentinel.write_text(spec.FREECAD_PIN, encoding="utf-8")
    else:
        receipt = spec.expected_receipt(external=True)
        receipt["vibecad_version"] = "0.3.0"
        sentinel.write_text(json.dumps(receipt), encoding="utf-8")

    assert status.runtime_recovery_kind() is status.RecoveryKind.REPAIR_REQUIRED


def test_external_receipt_binds_prefix_identity_and_never_writes_override(monkeypatch, tmp_path):
    home = tmp_path / "home"
    override = tmp_path / "external"
    python = status.paths.env_python_for(override)
    python.parent.mkdir(parents=True)
    python.write_bytes(b"python")
    payload = override / "payload.bin"
    payload.write_bytes(b"engine")
    before = (override.stat().st_ino, python.stat().st_ino, payload.read_bytes())
    monkeypatch.setenv("VIBECAD_HOME", str(home))
    monkeypatch.setenv("VIBECAD_FREECAD_ENV", str(override))

    status.write_external_runtime_receipt(override)

    assert status.runtime_ready() is True
    receipt = status.read_runtime_receipt()
    assert receipt == spec.expected_receipt(external=True)
    assert status.paths.ready_sentinel() == home / "runtime" / "external-runtime.json"
    assert not (override / ".vibecad_ready").exists()
    assert status.paths.external_runtime_receipt().stat().st_mode & 0o777 == 0o600
    bound = json.loads(status.paths.external_runtime_receipt().read_text(encoding="utf-8"))
    assert set(bound) == set(spec.expected_receipt(external=True)) | {
        "prefix",
        "prefix_device",
        "prefix_inode",
        "python_version",
        "freecad_version",
    }
    assert before == (override.stat().st_ino, python.stat().st_ino, payload.read_bytes())

    parked = tmp_path / "parked"
    override.rename(parked)
    override.mkdir()
    status.paths.env_python_for(override).parent.mkdir(parents=True)
    status.paths.env_python_for(override).touch()
    assert status.runtime_ready() is False


def test_pre_epoch_external_binding_is_incompatible_and_never_rewritten(
    monkeypatch,
    tmp_path,
):
    home = tmp_path / "home"
    override = tmp_path / "external"
    python = status.paths.env_python_for(override)
    python.parent.mkdir(parents=True)
    python.write_bytes(b"python")
    monkeypatch.setenv("VIBECAD_HOME", str(home))
    monkeypatch.setenv("VIBECAD_FREECAD_ENV", str(override))
    status.write_external_runtime_receipt(override)
    target = status.paths.external_runtime_receipt()
    assert status.runtime_receipt_state() is status.ReceiptState.CURRENT
    old_binding = json.loads(target.read_text(encoding="utf-8"))
    old_binding.pop("server_package_epoch", None)
    old_binding.pop("mcp_version", None)
    old_binding.pop("public_surface_sha256", None)
    original = json.dumps(old_binding, sort_keys=True)
    target.write_text(original, encoding="utf-8")

    assert status.runtime_receipt_state() is status.ReceiptState.INCOMPATIBLE
    assert status.runtime_recovery_kind() is status.RecoveryKind.REPAIR_REQUIRED
    assert status.read_runtime_receipt() is None
    assert target.read_text(encoding="utf-8") == original


@pytest.mark.parametrize("field", ("schema", "server_package_epoch"))
def test_external_binding_rejects_boolean_for_integer_identity(
    monkeypatch,
    tmp_path,
    field,
):
    home = tmp_path / "home"
    override = tmp_path / "external"
    python = status.paths.env_python_for(override)
    python.parent.mkdir(parents=True)
    python.write_bytes(b"python")
    monkeypatch.setenv("VIBECAD_HOME", str(home))
    monkeypatch.setenv("VIBECAD_FREECAD_ENV", str(override))
    status.write_external_runtime_receipt(override)
    target = status.paths.external_runtime_receipt()
    receipt = json.loads(target.read_text(encoding="utf-8"))
    receipt[field] = True
    malformed = json.dumps(receipt, sort_keys=True)
    target.write_text(malformed, encoding="utf-8")

    assert status.runtime_receipt_state() is status.ReceiptState.INCOMPATIBLE
    assert status.runtime_recovery_kind() is status.RecoveryKind.REPAIR_REQUIRED
    assert status.read_runtime_receipt() is None
    assert target.read_text(encoding="utf-8") == malformed


def test_external_receipt_is_never_read_through_a_runtime_data_alias(
    monkeypatch,
    tmp_path,
):
    home = tmp_path / "home"
    override = tmp_path / "external"
    python = status.paths.env_python_for(override)
    python.parent.mkdir(parents=True)
    python.write_bytes(b"python")
    monkeypatch.setenv("VIBECAD_HOME", str(home))
    monkeypatch.setenv("VIBECAD_FREECAD_ENV", str(override))
    status.write_external_runtime_receipt(override)
    runtime = home / "runtime"
    detached = home / "detached-runtime"
    receipt = (runtime / "external-runtime.json").read_bytes()
    runtime.rename(detached)
    data = home / "data"
    data.mkdir()
    (data / "external-runtime.json").write_bytes(receipt)
    runtime.symlink_to(data, target_is_directory=True)

    assert status.read_runtime_receipt() is None
    assert status.runtime_ready() is False
    assert (data / "external-runtime.json").read_bytes() == receipt


def test_external_receipt_rejects_prefix_symlink(monkeypatch, tmp_path):
    home = tmp_path / "home"
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)
    monkeypatch.setenv("VIBECAD_HOME", str(home))
    monkeypatch.setenv("VIBECAD_FREECAD_ENV", str(link))

    with pytest.raises(ValueError, match="symlink|符号链接"):
        status.write_external_runtime_receipt(link)


def test_legacy_receipt_with_symlink_ancestor_is_not_ownership_proof(monkeypatch, tmp_path):
    home = tmp_path / "home"
    outside = tmp_path / "outside-mamba"
    legacy = outside / "envs" / "vibecad"
    legacy.mkdir(parents=True)
    (legacy / ".vibecad_ready").write_text(
        json.dumps(spec.expected_receipt(), sort_keys=True), encoding="utf-8"
    )
    home.mkdir()
    (home / "mamba").symlink_to(outside, target_is_directory=True)
    monkeypatch.setenv("VIBECAD_HOME", str(home))

    assert status.managed_legacy_receipt(status.paths.legacy_env_prefix()) is None


def test_health_check_false_when_python_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(status.paths, "active_runtime_python", lambda: tmp_path / "nope")
    assert status.health_check() is False


def test_file_lock_exclusive_and_reentrant(tmp_path):
    lock = status.FileLock(tmp_path / "lock")
    with lock.acquire():
        assert status.FileLock(tmp_path / "lock").try_acquire() is False
    assert status.FileLock(tmp_path / "lock").try_acquire() is True


def test_file_lock_reclaims_dead_pid(tmp_path, monkeypatch):
    lock_dir = tmp_path / "lock"
    lock_dir.mkdir(mode=0o700)
    owner = lock_dir / "owner.json"
    owner.write_text(
        json.dumps({"pid": 2_000_000_000, "ts": 0, "token": "dead"}, sort_keys=True),
        encoding="utf-8",
    )
    owner.chmod(0o600)
    monkeypatch.setattr(status, "_pid_alive", lambda pid: False)
    replacement = status.FileLock(lock_dir)
    assert replacement.try_acquire() is True
    replacement._force_remove()


def test_file_lock_never_reclaims_an_old_but_live_owner(tmp_path, monkeypatch):
    lock_dir = tmp_path / "lock"
    lock = status.FileLock(lock_dir)
    assert lock.try_acquire() is True
    (lock_dir / "owner.json").write_text(json.dumps({"pid": 42, "ts": 0}))
    monkeypatch.setattr(status, "_pid_alive", lambda pid: pid == 42)

    assert status.FileLock(lock_dir).try_acquire() is False
    assert lock_dir.is_dir()


def test_file_lock_dual_dead_owner_reclaim_has_one_winner(tmp_path, monkeypatch):
    lock_dir = tmp_path / "lock"
    lock_dir.mkdir(mode=0o700)
    dead_pid = 2_000_000_000
    owner = lock_dir / "owner.json"
    owner.write_text(
        json.dumps({"pid": dead_pid, "ts": 0, "token": "dead-owner"}, sort_keys=True),
        encoding="utf-8",
    )
    owner.chmod(0o600)
    barrier = threading.Barrier(2)
    real_flock = status._try_exclusive_flock
    flock_calls = 0
    calls_guard = threading.Lock()

    def synchronized_flock(fd):
        nonlocal flock_calls
        with calls_guard:
            flock_calls += 1
            synchronize = flock_calls <= 2
        if synchronize:
            barrier.wait(timeout=2)
        return real_flock(fd)

    monkeypatch.setattr(status, "_try_exclusive_flock", synchronized_flock)
    monkeypatch.setattr(status, "_pid_alive", lambda pid: pid != dead_pid)
    locks = [status.FileLock(lock_dir), status.FileLock(lock_dir)]
    results: list[bool | None] = [None, None]

    def acquire(index, lock):
        results[index] = lock.try_acquire()

    workers = [
        threading.Thread(target=acquire, args=(index, lock)) for index, lock in enumerate(locks)
    ]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=3)

    assert all(not worker.is_alive() for worker in workers)
    assert sorted(value for value in results if value is not None) == [False, True]
    winner = locks[results.index(True)]
    winner._force_remove()
    assert not lock_dir.exists()


def test_file_lock_converges_from_dead_crash_left_reclaim_marker(tmp_path, monkeypatch):
    lock_dir = tmp_path / "lock"
    lock_dir.mkdir(mode=0o700)
    dead_owner = 2_000_000_000
    dead_reclaimer = 1_999_999_999
    for name, value in {
        "owner.json": {"pid": dead_owner, "ts": 0, "token": "old-owner"},
        ".reclaim": {"pid": dead_reclaimer, "ts": 0, "token": "old-claim"},
    }.items():
        path = lock_dir / name
        path.write_text(json.dumps(value, sort_keys=True), encoding="utf-8")
        path.chmod(0o600)
    monkeypatch.setattr(
        status,
        "_pid_alive",
        lambda pid: pid not in {dead_owner, dead_reclaimer},
    )

    replacement = status.FileLock(lock_dir)
    assert replacement.try_acquire() is True
    replacement._force_remove()
    assert not lock_dir.exists()


def test_file_lock_release_never_deletes_replacement_owner(tmp_path):
    lock_dir = tmp_path / "lock"
    lock = status.FileLock(lock_dir)
    assert lock.try_acquire() is True
    owner = lock_dir / "owner.json"
    owner.unlink()
    replacement = {"pid": os.getpid(), "ts": 0, "token": "replacement"}
    owner.write_text(json.dumps(replacement, sort_keys=True), encoding="utf-8")
    owner.chmod(0o600)

    lock._force_remove()

    assert lock_dir.is_dir()
    assert json.loads(owner.read_text(encoding="utf-8")) == replacement
    status.FileLock._force_remove_dir(lock_dir)


def test_file_lock_release_never_deletes_replacement_directory(tmp_path):
    lock_dir = tmp_path / "lock"
    lock = status.FileLock(lock_dir)
    assert lock.try_acquire() is True
    parked = tmp_path / "old-lock"
    lock_dir.rename(parked)
    lock_dir.mkdir(mode=0o700)
    replacement_owner = lock_dir / "owner.json"
    replacement_owner.write_text(
        json.dumps({"pid": os.getpid(), "ts": 0, "token": "replacement"}, sort_keys=True),
        encoding="utf-8",
    )
    replacement_owner.chmod(0o600)

    lock._force_remove()

    assert lock_dir.is_dir()
    assert json.loads(replacement_owner.read_text(encoding="utf-8"))["token"] == "replacement"
    status.FileLock._force_remove_dir(lock_dir)
    status.FileLock._force_remove_dir(parked)


def test_file_lock_wait_never_recreates_a_missing_parent(tmp_path):
    parent = tmp_path / "missing"
    lock = status.FileLock(parent / "lock")

    with pytest.raises(RuntimeError, match="超时"):
        with lock.acquire_wait(timeout=0):
            pytest.fail("missing lock parent must not be created by the wait loop")

    assert not parent.exists()


def test_windows_compatibility_path_keeps_status_receipt_and_lock_working(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setattr(status.sys, "platform", "win32")
    monkeypatch.setattr(status, "_secure_dir_fd_available", lambda: False)
    monkeypatch.setenv("VIBECAD_HOME", str(tmp_path))
    status.write_status(status.RuntimeStatus(message="fallback"))
    assert status.read_status().message == "fallback"

    prefix = status.paths.env_prefix()
    python = status.paths.env_python_for(prefix)
    python.parent.mkdir(parents=True)
    python.write_bytes(b"python")
    evidence = status.capture_runtime_generation_evidence(prefix)
    status.write_managed_runtime_receipt(prefix, evidence=evidence)
    assert status.read_runtime_receipt() == spec.expected_receipt()

    lock_path = tmp_path / "fallback.lock"
    with status.FileLock(lock_path).acquire():
        assert lock_path.is_dir()
    assert not lock_path.exists()


def test_windows_compatibility_path_rejects_obvious_parent_alias(monkeypatch, tmp_path):
    durable = tmp_path / "durable"
    durable.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(durable, target_is_directory=True)
    monkeypatch.setattr(status.sys, "platform", "win32")
    monkeypatch.setattr(status, "_secure_dir_fd_available", lambda: False)
    monkeypatch.setenv("VIBECAD_HOME", str(alias / "VibeCAD"))

    with pytest.raises(ValueError, match="alias"):
        status.write_status(status.RuntimeStatus(message="unsafe"))

    assert tuple(durable.iterdir()) == ()


def test_windows_fallback_converges_from_dead_reclaim_marker(monkeypatch, tmp_path):
    monkeypatch.setattr(status.sys, "platform", "win32")
    monkeypatch.setattr(status, "_secure_dir_fd_available", lambda: False)
    dead_owner = 2_000_000_000
    dead_claimant = 1_999_999_999
    lock_dir = tmp_path / "fallback.lock"
    lock_dir.mkdir(mode=0o700)
    for name, value in {
        "owner.json": {"pid": dead_owner, "ts": 0, "token": "old"},
        ".reclaim": {"pid": dead_claimant, "ts": 0, "token": "claim"},
    }.items():
        path = lock_dir / name
        path.write_text(json.dumps(value, sort_keys=True), encoding="utf-8")
        path.chmod(0o600)
    monkeypatch.setattr(
        status,
        "_pid_alive",
        lambda pid: pid not in {dead_owner, dead_claimant},
    )

    replacement = status.FileLock(lock_dir)
    assert replacement.try_acquire() is True
    replacement._force_remove()
    assert not lock_dir.exists()


def test_pid_alive_self_and_dead():
    import os  # B-1：跨平台探活（Windows 用 OpenProcess 而非杀进程的 os.kill）

    assert status._pid_alive(os.getpid()) is True
    assert status._pid_alive(2_000_000_000) is False
    assert status._pid_alive(None) is False


def test_prep_injects_freecad_module_path():
    # A1：conda-forge 把 FreeCAD.so 放 <prefix>/lib（Windows: Library/bin），须注入 sys.path
    assert "sys.path" in status._PREP
    assert "lib" in status._PREP
