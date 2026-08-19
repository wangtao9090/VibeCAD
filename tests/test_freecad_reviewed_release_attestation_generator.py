from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import os
import subprocess
import sys
import threading
import types
from pathlib import Path
from types import SimpleNamespace

import pytest

import vibecad.execution.freecad_reviewed_release_attestation_resource as packaged_resource

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / ".github/scripts/generate_freecad_reviewed_release_attestation.py"

_LOCK_HOLDER = """
import fcntl
import os
import sys

flags = (
    os.O_RDONLY
    | getattr(os, "O_CLOEXEC", 0)
    | getattr(os, "O_DIRECTORY", 0)
    | getattr(os, "O_NOFOLLOW", 0)
)
descriptor = os.open(sys.argv[1], flags)
mode = {"exclusive": fcntl.LOCK_EX, "shared": fcntl.LOCK_SH}[sys.argv[2]]
fcntl.flock(descriptor, mode)
sys.stdout.buffer.write(b"locked\\n")
sys.stdout.buffer.flush()
sys.stdin.buffer.read(1)
"""


@pytest.fixture(scope="module")
def generator():
    name = "_vibecad_test_reviewed_release_attestation_generator"
    spec = importlib.util.spec_from_file_location(name, SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _bind_fixed_targets(generator, monkeypatch: pytest.MonkeyPatch, root: Path):
    directory = root / "src/vibecad/execution/_attestations"
    directory.mkdir(parents=True)
    x86_resource = directory / generator._RESOURCE_NAME_BY_PLATFORM_ID["macos.x86_64"]
    arm_resource = directory / generator._RESOURCE_NAME_BY_PLATFORM_ID["macos.arm64"]
    pins = directory / "freecad_reviewed_release_attestation_pins.py"
    monkeypatch.setattr(generator, "_ATTESTATION_DIRECTORY", directory)
    monkeypatch.setattr(generator, "_PINS_PATH", pins)
    return directory, x86_resource, arm_resource, pins


def _start_lock_holder(directory: Path, mode: str) -> subprocess.Popen[bytes]:
    process = subprocess.Popen(
        [sys.executable, "-c", _LOCK_HOLDER, str(directory), mode],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert process.stdout is not None
    ready = process.stdout.readline()
    if ready != b"locked\n":
        _stdout, stderr = process.communicate(timeout=5)
        raise AssertionError(f"lock holder failed: {stderr.decode('utf-8', errors='replace')}")
    return process


def _release_lock_holder(process: subprocess.Popen[bytes]) -> None:
    assert process.stdin is not None
    process.stdin.write(b"x")
    process.stdin.flush()
    process.stdin.close()
    assert process.wait(timeout=5) == 0
    assert process.stderr is not None
    assert process.stderr.read() == b""


def test_fixed_platform_resource_names_match_the_runtime_loader(generator) -> None:
    assert generator._RESOURCE_NAME_BY_PLATFORM_ID == dict(
        packaged_resource.FREECAD_REVIEWED_RELEASE_ATTESTATION_RESOURCE_NAME_BY_PLATFORM_ID
    )


@pytest.mark.parametrize(
    ("holder_mode", "waiter_exclusive"),
    [
        ("exclusive", True),
        ("exclusive", False),
        ("shared", True),
    ],
)
def test_directory_lock_serializes_writers_and_excludes_checks_during_publication(
    generator,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    holder_mode: str,
    waiter_exclusive: bool,
) -> None:
    directory, _x86_resource, _arm_resource, _pins = _bind_fixed_targets(
        generator, monkeypatch, tmp_path
    )
    holder = _start_lock_holder(directory, holder_mode)
    started = threading.Event()
    acquired = threading.Event()
    failures: list[BaseException] = []

    def wait_for_lock() -> None:
        started.set()
        try:
            with generator._attestation_index_lock(exclusive=waiter_exclusive):
                acquired.set()
        except BaseException as exc:
            failures.append(exc)

    waiter = threading.Thread(target=wait_for_lock, daemon=True)
    waiter.start()
    assert started.wait(timeout=2)
    try:
        assert not acquired.wait(timeout=0.5)
    finally:
        _release_lock_holder(holder)
    assert acquired.wait(timeout=5)
    waiter.join(timeout=5)
    assert not waiter.is_alive()
    assert failures == []


def test_directory_lock_allows_concurrent_checks(
    generator,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    directory, _x86_resource, _arm_resource, _pins = _bind_fixed_targets(
        generator, monkeypatch, tmp_path
    )
    holder = _start_lock_holder(directory, "shared")
    acquired = threading.Event()
    failures: list[BaseException] = []

    def share_lock() -> None:
        try:
            with generator._attestation_index_lock(exclusive=False):
                acquired.set()
        except BaseException as exc:
            failures.append(exc)

    waiter = threading.Thread(target=share_lock, daemon=True)
    waiter.start()
    try:
        assert acquired.wait(timeout=5)
    finally:
        _release_lock_holder(holder)
    waiter.join(timeout=5)
    assert not waiter.is_alive()
    assert failures == []


@pytest.mark.parametrize(
    ("check", "expected_exclusive", "expected_changed", "expected_mode"),
    [
        (False, True, True, "generate"),
        (True, False, False, "check"),
    ],
)
def test_current_platform_pair_transaction_holds_the_required_lock_mode(
    generator,
    monkeypatch: pytest.MonkeyPatch,
    check: bool,
    expected_exclusive: bool,
    expected_changed: bool,
    expected_mode: str,
) -> None:
    active_lock: list[bool] = []
    events: list[str] = []
    result = SimpleNamespace(resource=b"result")

    @contextlib.contextmanager
    def lock(*, exclusive: bool):
        assert active_lock == []
        active_lock.append(exclusive)
        events.append("lock-enter")
        try:
            yield
        finally:
            events.append("lock-exit")
            active_lock.clear()

    def select(_result):
        assert active_lock == [expected_exclusive]
        events.append("select")
        return "macos.x86_64", Path("resource.json"), b"pins"

    def check_pair(**_kwargs):
        assert check is True
        assert active_lock == [False]
        events.append("check")

    def publish_pair(**_kwargs):
        assert check is False
        assert active_lock == [True]
        events.append("generate")
        return True

    monkeypatch.setattr(generator, "_attestation_index_lock", lock)
    monkeypatch.setattr(generator, "_current_platform_publication", select)
    monkeypatch.setattr(generator, "_check_pair", check_pair)
    monkeypatch.setattr(generator, "_publish_pair", publish_pair)

    changed, mode = generator._apply_current_platform_result(result, check=check)

    assert changed is expected_changed
    assert mode == expected_mode
    assert events == ["lock-enter", "select", expected_mode, "lock-exit"]


def test_pin_source_is_canonical_sorted_and_round_trips(generator) -> None:
    mapping = {
        ("0.10.1", "macos.x86_64"): hashlib.sha256(b"later").hexdigest(),
        ("0.10.0", "macos.x86_64"): hashlib.sha256(b"current").hexdigest(),
        ("0.10.0", "macos.arm64"): hashlib.sha256(b"arm").hexdigest(),
    }

    raw = generator._render_pins(mapping)

    assert generator._decode_canonical_pins(raw) == mapping
    assert raw.index(b'"macos.arm64"') < raw.index(b'"macos.x86_64"')
    assert raw.index(b'"macos.x86_64"') < raw.rindex(b'"0.10.1"')
    checked_in = (
        ROOT / "src/vibecad/execution/_attestations/freecad_reviewed_release_attestation_pins.py"
    ).read_bytes()
    assert generator._render_pins(generator._decode_canonical_pins(checked_in)) == checked_in


def test_check_mode_compares_exact_bytes_without_any_write(
    generator,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _directory, resource, _arm_resource, pins = _bind_fixed_targets(
        generator, monkeypatch, tmp_path
    )
    expected_resource = b'{"canonical":true}'
    expected_pins = generator._render_pins(
        {("0.10.0", "macos.x86_64"): hashlib.sha256(expected_resource).hexdigest()}
    )
    resource.write_bytes(expected_resource)
    pins.write_bytes(expected_pins)
    before = {
        path: (path.read_bytes(), path.stat().st_ino, path.stat().st_mtime_ns)
        for path in (resource, pins)
    }
    monkeypatch.setattr(generator, "_decode_canonical_resource", lambda raw: None)
    monkeypatch.setattr(
        generator,
        "_stage_file",
        lambda **_kwargs: pytest.fail("--check must not stage a file"),
    )
    monkeypatch.setattr(
        generator.os,
        "replace",
        lambda *_args: pytest.fail("--check must not replace a file"),
    )

    generator._check_pair(
        resource_path=resource,
        resource=expected_resource,
        pins=expected_pins,
    )

    assert before == {
        path: (path.read_bytes(), path.stat().st_ino, path.stat().st_mtime_ns)
        for path in (resource, pins)
    }


def test_pair_publication_restores_the_old_resource_if_the_pin_replace_fails(
    generator,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    directory, resource, _arm_resource, pins = _bind_fixed_targets(generator, monkeypatch, tmp_path)
    old_resource = b'{"release":"old"}'
    new_resource = b'{"release":"new"}'
    old_pins = generator._render_pins({})
    new_pins = generator._render_pins(
        {("0.10.0", "macos.x86_64"): hashlib.sha256(new_resource).hexdigest()}
    )
    resource.write_bytes(old_resource)
    pins.write_bytes(old_pins)
    monkeypatch.setattr(generator, "_decode_canonical_resource", lambda raw: None)
    real_replace = os.replace
    replace_count = 0

    def fail_second_replace(source, target):
        nonlocal replace_count
        replace_count += 1
        if replace_count == 2:
            raise OSError("injected pin publication failure")
        real_replace(source, target)

    monkeypatch.setattr(generator.os, "replace", fail_second_replace)

    with pytest.raises(generator.GenerationError, match="both files were restored"):
        generator._publish_pair(
            resource_path=resource,
            resource=new_resource,
            pins=new_pins,
        )

    assert replace_count == 3
    assert resource.read_bytes() == old_resource
    assert pins.read_bytes() == old_pins
    assert {path.name for path in directory.iterdir()} == {resource.name, pins.name}


def test_arm_publication_preserves_the_x86_resource_and_sibling_pin(
    generator,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _directory, x86_resource, arm_resource, pins = _bind_fixed_targets(
        generator, monkeypatch, tmp_path
    )
    x86_raw = b'{"platform":"x86"}'
    arm_raw = b'{"platform":"arm"}'
    x86_digest = hashlib.sha256(x86_raw).hexdigest()
    arm_digest = hashlib.sha256(arm_raw).hexdigest()
    x86_resource.write_bytes(x86_raw)
    pins.write_bytes(generator._render_pins({("0.10.0", "macos.x86_64"): x86_digest}))
    monkeypatch.setattr(generator, "_platform_id", lambda: "macos.arm64")
    monkeypatch.setattr(generator, "_decode_canonical_resource", lambda raw: None)
    result = SimpleNamespace(
        release_version="0.10.0",
        resource_sha256=arm_digest,
        runtime_platform_id="macos.arm64",
    )

    platform_id, selected_resource, updated_pins = generator._current_platform_publication(result)
    changed = generator._publish_pair(
        resource_path=selected_resource,
        resource=arm_raw,
        pins=updated_pins,
    )

    assert changed is True
    assert platform_id == "macos.arm64"
    assert selected_resource == arm_resource
    assert arm_resource.read_bytes() == arm_raw
    assert x86_resource.read_bytes() == x86_raw
    assert generator._decode_canonical_pins(pins.read_bytes()) == {
        ("0.10.0", "macos.arm64"): arm_digest,
        ("0.10.0", "macos.x86_64"): x86_digest,
    }


@pytest.mark.parametrize(
    ("trusted_platform", "observed_platform", "message"),
    [
        ("linux.x86_64", "linux.x86_64", "no fixed attestation resource"),
        ("macos.arm64", "macos.x86_64", "does not match"),
    ],
)
def test_publication_rejects_unsupported_or_discovery_selected_platforms_before_pin_read(
    generator,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    trusted_platform: str,
    observed_platform: str,
    message: str,
) -> None:
    _directory, _x86_resource, _arm_resource, _pins = _bind_fixed_targets(
        generator, monkeypatch, tmp_path
    )
    monkeypatch.setattr(generator, "_platform_id", lambda: trusted_platform)
    result = SimpleNamespace(
        release_version="0.10.0",
        resource_sha256="a" * 64,
        runtime_platform_id=observed_platform,
    )

    with pytest.raises(generator.GenerationError, match=message):
        generator._current_platform_publication(result)


def test_fixed_outputs_reject_symlinks_and_noncanonical_pin_source(
    generator,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _directory, resource, _arm_resource, pins = _bind_fixed_targets(
        generator, monkeypatch, tmp_path
    )
    target = tmp_path / "outside.json"
    target.write_bytes(b"{}")
    resource.symlink_to(target)

    with pytest.raises(generator.GenerationError, match="must not traverse a symlink"):
        generator._read_fixed_file(resource, required=True)

    pins.write_bytes(generator._render_pins({}) + b"\n")
    with pytest.raises(generator.GenerationError, match="not canonical"):
        generator._read_fixed_file(pins, required=True)


def test_generate_can_transactionally_replace_a_canonical_stale_catalog_resource(
    generator,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _directory, resource, _arm_resource, pins = _bind_fixed_targets(
        generator, monkeypatch, tmp_path
    )
    stale = generator._canonical_json(
        {
            "schema_version": 1,
            "verification_set": {"formal_operation_count": 124},
        }
    )
    resource.write_bytes(stale)
    pins.write_bytes(generator._render_pins({}))
    monkeypatch.setattr(
        generator,
        "decode_freecad_reviewed_release_attestation",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(ValueError("stale catalog")),
    )

    assert generator._read_fixed_file(resource, required=True) == stale
    with pytest.raises(generator.GenerationError, match="not canonical"):
        generator._decode_canonical_resource(stale)


@pytest.mark.parametrize(
    "raw",
    (
        b'{"a":1,"a":2}',
        b'{"a":1}\n',
        b"[]",
        b'{"value":NaN}',
    ),
)
def test_historical_resource_migration_still_requires_bounded_canonical_json(
    generator,
    raw: bytes,
) -> None:
    with pytest.raises(generator.GenerationError, match="structurally canonical"):
        generator._decode_structural_canonical_resource(raw)


def test_real_builder_sequence_is_discovery_then_exact_current_verification_and_codec(
    generator,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    backend = object()
    snapshot = SimpleNamespace(
        backend=backend,
        snapshot_sha256="a" * 64,
        platform_id="macos.arm64",
    )
    discovery = SimpleNamespace(
        snapshot=snapshot,
        manifest=SimpleNamespace(manifest_sha256="b" * 64),
    )
    verification_set = SimpleNamespace(
        runtime_backend=backend,
        receipts=tuple(range(generator.CURRENT_MANAGED_VERIFICATION_RECEIPT_COUNT)),
        formal_operations=tuple(
            range(generator.CURRENT_MANAGED_VERIFICATION_FORMAL_OPERATION_COUNT)
        ),
        native_types=tuple(range(generator.CURRENT_MANAGED_VERIFICATION_NATIVE_TYPE_COUNT)),
    )
    attestation = SimpleNamespace(attestation_sha256="c" * 64)
    encoded = b'{"attestation":"canonical"}'
    decoded = object()
    fake_freecad = types.ModuleType("FreeCAD")
    fake_freecad.GuiUp = 0
    fake_freecad.listDocuments = lambda: {}
    fake_freecad.getUserCachePath = lambda: os.environ["FREECAD_USER_TEMP"]
    monkeypatch.setitem(sys.modules, "FreeCAD", fake_freecad)
    monkeypatch.delitem(sys.modules, "FreeCADGui", raising=False)
    monkeypatch.setenv("FREECAD_USER_TEMP", "caller-value")

    import vibecad.freecad_env as freecad_env

    monkeypatch.setattr(
        freecad_env,
        "prepare_freecad_import",
        lambda: events.append("prepare"),
    )

    def collect(*, freecad, probe_modules):
        assert freecad is fake_freecad
        assert probe_modules == generator.FREECAD_DISCOVERY_V2_ALLOWED_MODULES
        native_root = Path(os.environ["FREECAD_USER_TEMP"])
        assert native_root != Path("caller-value")
        assert native_root.is_dir()
        assert native_root.stat().st_mode & 0o777 == 0o700
        events.append("discover")
        return discovery

    def verify(*, freecad):
        assert freecad is fake_freecad
        events.append("verify")
        return verification_set

    def build(**kwargs):
        assert kwargs["runtime_backend"] is backend
        assert kwargs["verification_set"] is verification_set
        events.append("build")
        return attestation

    def encode(value):
        assert value is attestation
        events.append("encode")
        return encoded

    def decode(raw, *, expected_source_attestation_sha256):
        assert raw == encoded
        assert expected_source_attestation_sha256 == hashlib.sha256(encoded).hexdigest()
        events.append("decode")
        return decoded

    def validate(value, **kwargs):
        assert value is decoded
        assert kwargs["runtime_backend"] is backend
        events.append("validate")
        return value

    monkeypatch.setattr(generator, "collect_managed_freecad_discovery_v2", collect)
    monkeypatch.setattr(
        generator,
        "build_current_managed_freecad_reviewed_verification_set_for_maintainers",
        verify,
    )
    monkeypatch.setattr(generator, "build_freecad_reviewed_release_attestation", build)
    monkeypatch.setattr(generator, "encode_freecad_reviewed_release_attestation", encode)
    monkeypatch.setattr(generator, "decode_freecad_reviewed_release_attestation", decode)
    monkeypatch.setattr(generator, "validate_freecad_reviewed_release_attestation", validate)

    result = generator._build_release_attestation()

    assert events == ["prepare", "discover", "verify", "build", "encode", "decode", "validate"]
    assert result.resource == encoded
    assert result.receipt_count == 21
    assert result.formal_operation_count == 126
    assert result.native_type_count == 102
    assert os.environ["FREECAD_USER_TEMP"] == "caller-value"
    assert fake_freecad.listDocuments() == {}
    assert "FreeCADGui" not in sys.modules


def test_cli_has_no_output_path_override(generator, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        generator,
        "_build_release_attestation",
        lambda: pytest.fail("argument rejection must happen before FreeCAD starts"),
    )
    with pytest.raises(SystemExit) as raised:
        generator.main(["--output", "/tmp/forbidden"])
    assert raised.value.code == 2
