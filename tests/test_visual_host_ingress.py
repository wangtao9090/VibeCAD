from __future__ import annotations

import contextlib
import hashlib
import json
import os
import shutil
import sys
import tempfile
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

import vibecad.daemon.client as daemon_client_module
from vibecad._file_compat import (
    capture_windows_fd,
    ensure_private_directory,
    open_windows_directory_fd,
    set_private_dacl,
    windows_extended_path,
)
from vibecad._file_compat import (
    pread as portable_pread,
)
from vibecad.application.agent import AgentApplication
from vibecad.application.visual_ingress import (
    VisualIngressError,
    bind_visual_staging_locator,
    open_visual_staging,
    parse_seal_image_set_request,
    validate_seal_result,
)
from vibecad.daemon.adapters import (
    LocalAgentClient,
    LocalAgentClientError,
    LocalAgentClientErrorCode,
)
from vibecad.daemon.client import LocalKernelClient
from vibecad.daemon.facade import LocalKernelFacade
from vibecad.daemon.service import LocalKernelDaemon
from vibecad.interaction.protocol_v2 import (
    StaticV2Dispatcher,
    V2ErrorCode,
    V2ProtocolError,
    V2Request,
    V2Response,
)


def _request(*, count: int = 1) -> dict[str, object]:
    roles = ("front", "top", "right", "isometric")
    return {
        "schema_version": 1,
        "create_key": "image_set_create_" + "1" * 32,
        "inputs": [
            {
                "schema_version": 1,
                "view_role": roles[index],
                "calibration_status": "unknown",
                "declared_mime": "image/png",
            }
            for index in range(count)
        ],
        "unit": "mm",
        "dimension_hints": [],
        "calibration_evidence": [],
        "same_object": True,
        "same_state": True,
        "same_scale": True,
        "processing_authorization": "local_only",
    }


def _png(path: Path, color: tuple[int, int, int]) -> None:
    Image.new("RGB", (8, 6), color).save(path, format="PNG")
    path.chmod(0o600)


def _stage(tmp_path: Path, *, count: int = 1):
    root = tmp_path / "stage"
    if sys.platform == "win32":
        ensure_private_directory(root)
    else:
        root.mkdir(mode=0o700)
        root.chmod(0o700)
    for index in range(count):
        _png(root / f"source_{index}", (20 + index, 80, 140))
        if sys.platform == "win32":
            set_private_dacl(root / f"source_{index}")
    if sys.platform == "win32":
        descriptor = open_windows_directory_fd(root)
    else:
        descriptor = os.open(
            root,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
        )
    request = parse_seal_image_set_request(_request(count=count))
    if sys.platform == "win32":
        stats = tuple(os.stat(root / f"source_{index}") for index in range(count))
    else:
        stats = tuple(
            os.stat(f"source_{index}", dir_fd=descriptor, follow_symlinks=False)
            for index in range(count)
        )
    source_sha256 = tuple(
        hashlib.sha256((root / f"source_{index}").read_bytes()).hexdigest()
        for index in range(count)
    )
    locator = bind_visual_staging_locator(
        request,
        os.fstat(descriptor),
        stats,
        source_sha256,
    )
    return root, descriptor, request, locator


def test_host_request_parser_is_exact_and_adapter_rejects_path_before_kernel() -> None:
    request = _request()
    parsed = parse_seal_image_set_request(request)
    assert parsed.to_mapping() == request

    with pytest.raises(VisualIngressError):
        parse_seal_image_set_request(request | {"source_path": "/private/input.png"})

    class Kernel:
        calls = 0

        def call(self, *_args, **_kwargs):
            self.calls += 1
            raise AssertionError("invalid host request reached the local wire")

        def seal_visual_image_set(self, *_args, **_kwargs):
            self.calls += 1
            raise AssertionError("invalid host request reached staging")

        def close(self) -> None:
            pass

    kernel = Kernel()
    client = LocalAgentClient(kernel)
    with pytest.raises(LocalAgentClientError) as raised:
        client.seal_visual_image_set_request(
            request | {"source_path": "/private/input.png"},
            source_paths=("/private/input.png",),
        )
    assert raised.value.code is LocalAgentClientErrorCode.INVALID_INPUT
    assert kernel.calls == 0

    with pytest.raises(VisualIngressError):
        validate_seal_result(
            {
                "schema_version": True,
                "image_set_id": "image_set_" + "1" * 32,
                "image_set_manifest_sha256": "2" * 64,
            }
        )


def test_daemon_staging_opens_only_fixed_unique_regular_sources(tmp_path: Path) -> None:
    root, descriptor, request, locator = _stage(tmp_path, count=2)
    try:
        opened = open_visual_staging(request, descriptor, locator)
        try:
            opened.verify()
            assert len(opened.sources) == 2
            assert [portable_pread(item.fd, 8, 0) for item in opened.sources] == [
                b"\x89PNG\r\n\x1a\n",
                b"\x89PNG\r\n\x1a\n",
            ]
        finally:
            opened.close()

        (root / "unexpected").write_bytes(b"not admitted")
        (root / "unexpected").chmod(0o600)
        with pytest.raises(VisualIngressError):
            open_visual_staging(request, descriptor, locator)
    finally:
        os.close(descriptor)


def test_daemon_staging_rejects_bytes_not_matching_client_copy_digest(tmp_path: Path) -> None:
    root = tmp_path / "digest-stage"
    if sys.platform == "win32":
        ensure_private_directory(root)
    else:
        root.mkdir(mode=0o700)
    source = root / "source_0"
    _png(source, (10, 20, 30))
    expected_sha256 = hashlib.sha256(source.read_bytes()).hexdigest()
    _png(source, (200, 40, 50))
    if sys.platform == "win32":
        set_private_dacl(source)
        descriptor = open_windows_directory_fd(root)
        source_stat = os.stat(source)
    else:
        descriptor = os.open(
            root,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
        )
        source_stat = os.stat("source_0", dir_fd=descriptor, follow_symlinks=False)
    request = parse_seal_image_set_request(_request())
    locator = bind_visual_staging_locator(
        request,
        os.fstat(descriptor),
        (source_stat,),
        (expected_sha256,),
    )
    try:
        with pytest.raises(VisualIngressError):
            open_visual_staging(request, descriptor, locator)
    finally:
        os.close(descriptor)


def test_bad_declared_png_is_invalid_input_not_daemon_unavailable(tmp_path: Path) -> None:
    stage = tmp_path / "bad-stage"
    if sys.platform == "win32":
        ensure_private_directory(stage)
    else:
        stage.mkdir(mode=0o700)
        stage.chmod(0o700)
    source = stage / "source_0"
    source.write_bytes(b"not-a-png")
    source.chmod(0o600)
    if sys.platform == "win32":
        set_private_dacl(source)
        descriptor = open_windows_directory_fd(stage)
        source_stat = os.stat(source)
    else:
        descriptor = os.open(
            stage,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
        )
        source_stat = os.stat("source_0", dir_fd=descriptor, follow_symlinks=False)
    request = parse_seal_image_set_request(_request())
    locator = bind_visual_staging_locator(
        request,
        os.fstat(descriptor),
        (source_stat,),
        (hashlib.sha256(source.read_bytes()).hexdigest(),),
    )
    application = AgentApplication.open(data_root=tmp_path / "bad-data")
    facade = LocalKernelFacade(application, daemon_id="daemon_" + "4" * 32)
    try:
        with pytest.raises(V2ProtocolError) as raised:
            facade._visual_inputs_seal(  # noqa: SLF001
                {"request": request.to_mapping(), "locator": locator},
                descriptor,
            )
        assert raised.value.code is V2ErrorCode.INVALID_REQUEST
    finally:
        os.close(descriptor)
        facade.close()
        application.close()

    class RejectedKernel:
        def call(self, *_args, **_kwargs):
            raise AssertionError("generic application.call must not be used")

        def seal_visual_image_set(self, *_args, **_kwargs) -> V2Response:
            return V2Response(
                request_id="request_" + "5" * 32,
                sequence=1,
                result=None,
                error={"code": "invalid_request", "message": "redacted"},
            )

        def close(self) -> None:
            pass

    client = LocalAgentClient(RejectedKernel())
    with pytest.raises(LocalAgentClientError) as rejected:
        client.seal_visual_image_set_request(
            _request(),
            source_paths=(str(source),),
        )
    assert rejected.value.code is LocalAgentClientErrorCode.INVALID_INPUT


@pytest.mark.parametrize(
    "mutation",
    [
        "missing",
        "swap",
        "symlink",
        pytest.param(
            "fifo",
            marks=pytest.mark.skipif(
                sys.platform == "win32",
                reason="named FIFO filesystem entries are POSIX-specific",
            ),
        ),
    ],
)
def test_daemon_staging_rejects_entry_changes_without_blocking(
    tmp_path: Path,
    mutation: str,
) -> None:
    root, descriptor, request, locator = _stage(tmp_path)
    source = root / "source_0"
    try:
        source.unlink()
        if mutation == "swap":
            _png(source, (200, 20, 20))
        elif mutation == "symlink":
            target = root / "target"
            _png(target, (30, 30, 30))
            source.symlink_to(target.name)
        elif mutation == "fifo":
            os.mkfifo(source, 0o600)
        with pytest.raises(VisualIngressError):
            open_visual_staging(request, descriptor, locator)
    finally:
        os.close(descriptor)


def test_visual_descriptor_dispatch_requires_one_directory_fd() -> None:
    calls: list[int] = []
    dispatcher = StaticV2Dispatcher(
        visual_inputs_seal=lambda _params, descriptor: calls.append(descriptor) or {},
    )
    request = V2Request(
        request_id="request_" + "1" * 32,
        sequence=1,
        method="visual_inputs.seal",
        params={"request": {}, "locator": {}},
    )
    with pytest.raises(V2ProtocolError) as raised:
        dispatcher.dispatch(request)
    assert raised.value.code is V2ErrorCode.INVALID_REQUEST
    assert dispatcher.dispatch(request, descriptor=7) == {}
    assert calls == [7]


def test_low_level_host_ingress_is_path_free_replay_safe_and_cleans_staging(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "front.png"
    _png(source, (20, 80, 140))
    managed = tmp_path / "managed"
    if sys.platform == "win32":
        ensure_private_directory(managed)
    else:
        managed.mkdir(mode=0o700)
    run_root = managed / "daemon"
    if sys.platform == "win32":
        ensure_private_directory(run_root)
    else:
        run_root.mkdir(mode=0o700)
    application = AgentApplication.open(data_root=tmp_path / "data")
    facade = LocalKernelFacade(application, daemon_id="daemon_" + "2" * 32)
    observed_params: list[dict[str, object]] = []
    staging_paths: list[Path] = []
    original_mkdtemp = daemon_client_module.tempfile.mkdtemp
    original_windows_stage = daemon_client_module._create_windows_visual_stage

    def captured_mkdtemp(*args, **kwargs) -> str:
        value = original_mkdtemp(*args, **kwargs)
        staging_paths.append(Path(value))
        return value

    def captured_windows_stage(*args, **kwargs):
        value = original_windows_stage(*args, **kwargs)
        staging_paths.append(value[0])
        return value

    def local_dispatch(
        _self,
        method,
        params,
        *,
        request_id,
        descriptor,
        timeout_seconds=None,
    ) -> V2Response:
        assert method == "visual_inputs.seal"
        assert timeout_seconds is None
        assert descriptor is not None
        if sys.platform == "win32":
            capability = capture_windows_fd(descriptor, directory=True)
            names = [entry.name for entry in os.scandir(windows_extended_path(capability.path))]
        else:
            names = os.listdir(descriptor)
        assert names == ["source_0"]
        raw = json.dumps(params, sort_keys=True)
        assert str(source) not in raw
        assert "source_0" not in raw
        assert not any(type(value) is bytes for value in params.values())
        observed_params.append(params)
        try:
            result = facade._visual_inputs_seal(params, descriptor)  # noqa: SLF001
        except V2ProtocolError as error:
            return V2Response(
                request_id=request_id or "request_" + "3" * 32,
                sequence=1,
                result=None,
                error={"code": error.code.value, "message": "bounded failure"},
            )
        return V2Response(
            request_id=request_id or "request_" + "3" * 32,
            sequence=1,
            result=result,
            error=None,
        )

    monkeypatch.setattr(daemon_client_module.tempfile, "mkdtemp", captured_mkdtemp)
    monkeypatch.setattr(
        daemon_client_module,
        "_create_windows_visual_stage",
        captured_windows_stage,
    )
    monkeypatch.setattr(LocalKernelClient, "_call", local_dispatch)
    client = object.__new__(LocalKernelClient)
    client._boot_state = SimpleNamespace(root=SimpleNamespace(path=run_root))  # noqa: SLF001
    client._connection = None  # noqa: SLF001
    client._creator_pid = os.getpid()  # noqa: SLF001
    client._lock = threading.Lock()  # noqa: SLF001
    client._protocol = None  # noqa: SLF001
    client._reader = None  # noqa: SLF001
    client._closed = False  # noqa: SLF001
    original_bytes = source.read_bytes()
    try:
        first = client.seal_visual_image_set(_request(), source_paths=(str(source),))
        second = client.seal_visual_image_set(_request(), source_paths=(str(source),))
        _png(source, (200, 20, 20))
        adapter = LocalAgentClient(client)
        with pytest.raises(LocalAgentClientError) as conflict:
            adapter.seal_visual_image_set_request(
                _request(),
                source_paths=(str(source),),
            )
        assert conflict.value.code is LocalAgentClientErrorCode.INVALID_INPUT
        source.write_bytes(original_bytes)
        source.chmod(0o600)
        restored = client.seal_visual_image_set(_request(), source_paths=(str(source),))
    finally:
        facade.close()
        application.close()

    assert first.error is None and second.error is None
    assert first.result == second.result == restored.result
    assert set(first.result) == {
        "schema_version",
        "image_set_id",
        "image_set_manifest_sha256",
    }
    assert len(observed_params) == 4
    assert len(staging_paths) == 4
    assert all(not path.exists() for path in staging_paths)
    assert source.exists()


def test_duplicate_source_identity_is_rejected_and_stage_is_cleaned(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "same.png"
    _png(source, (10, 20, 30))
    managed = tmp_path / "managed"
    if sys.platform == "win32":
        ensure_private_directory(managed)
    else:
        managed.mkdir(mode=0o700)
    run_root = managed / "daemon"
    if sys.platform == "win32":
        ensure_private_directory(run_root)
    else:
        run_root.mkdir(mode=0o700)
    client = object.__new__(LocalKernelClient)
    client._boot_state = SimpleNamespace(root=SimpleNamespace(path=run_root))  # noqa: SLF001
    client._connection = None  # noqa: SLF001
    client._creator_pid = os.getpid()  # noqa: SLF001
    client._lock = threading.Lock()  # noqa: SLF001
    client._protocol = None  # noqa: SLF001
    client._reader = None  # noqa: SLF001
    client._closed = False  # noqa: SLF001
    called = False

    def must_not_call(*_args, **_kwargs):
        nonlocal called
        called = True
        raise AssertionError("duplicate source reached the local wire")

    monkeypatch.setattr(LocalKernelClient, "_call", must_not_call)
    with pytest.raises(daemon_client_module.LocalVisualSourceError):
        client.seal_visual_image_set(
            _request(count=2),
            source_paths=(str(source), str(source)),
        )
    assert called is False


def test_renamed_staged_bytes_turn_known_result_into_bounded_cleanup_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "cleanup.png"
    _png(source, (10, 40, 70))
    managed = tmp_path / "managed-cleanup"
    if sys.platform == "win32":
        ensure_private_directory(managed)
    else:
        managed.mkdir(mode=0o700)
    run_root = managed / "daemon"
    if sys.platform == "win32":
        ensure_private_directory(run_root)
    else:
        run_root.mkdir(mode=0o700)
    staging_paths: list[Path] = []
    original_mkdtemp = daemon_client_module.tempfile.mkdtemp
    original_windows_stage = daemon_client_module._create_windows_visual_stage

    def captured_mkdtemp(*args, **kwargs) -> str:
        value = original_mkdtemp(*args, **kwargs)
        staging_paths.append(Path(value))
        return value

    def captured_windows_stage(*args, **kwargs):
        value = original_windows_stage(*args, **kwargs)
        staging_paths.append(value[0])
        return value

    def rename_before_cleanup(
        _self,
        _method,
        _params,
        *,
        request_id,
        descriptor,
        timeout_seconds=None,
    ) -> V2Response:
        assert timeout_seconds is None
        if sys.platform == "win32":
            capability = capture_windows_fd(descriptor, directory=True)
            os.rename(
                Path(capability.path) / "source_0",
                Path(capability.path) / "kept",
            )
        else:
            os.rename(
                "source_0",
                "kept",
                src_dir_fd=descriptor,
                dst_dir_fd=descriptor,
            )
        return V2Response(
            request_id=request_id or "request_" + "6" * 32,
            sequence=1,
            result={
                "schema_version": 1,
                "image_set_id": "image_set_" + "7" * 32,
                "image_set_manifest_sha256": "8" * 64,
            },
            error=None,
        )

    monkeypatch.setattr(daemon_client_module.tempfile, "mkdtemp", captured_mkdtemp)
    monkeypatch.setattr(
        daemon_client_module,
        "_create_windows_visual_stage",
        captured_windows_stage,
    )
    monkeypatch.setattr(LocalKernelClient, "_call", rename_before_cleanup)
    client = object.__new__(LocalKernelClient)
    client._boot_state = SimpleNamespace(root=SimpleNamespace(path=run_root))  # noqa: SLF001
    client._connection = None  # noqa: SLF001
    client._creator_pid = os.getpid()  # noqa: SLF001
    client._lock = threading.Lock()  # noqa: SLF001
    client._protocol = None  # noqa: SLF001
    client._reader = None  # noqa: SLF001
    client._closed = False  # noqa: SLF001
    try:
        with pytest.raises(daemon_client_module.DaemonError):
            client.seal_visual_image_set(_request(), source_paths=(str(source),))
        assert len(staging_paths) == 1
        assert (staging_paths[0] / "kept").is_file()
    finally:
        for path in staging_paths:
            shutil.rmtree(path, ignore_errors=True)


@pytest.mark.skipif(
    sys.platform not in {"darwin", "win32"},
    reason="authenticated local daemon is supported on macOS and Windows",
)
def test_authenticated_daemon_host_adapter_seals_and_replays_real_png(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base = Path(
        tempfile.mkdtemp(
            prefix="vibecad-vh-",
            dir=os.path.realpath(tempfile.gettempdir()),
        )
    )
    base.chmod(0o700)
    sources = tuple(base / f"view_{index}.png" for index in range(4))
    for index, source in enumerate(sources):
        _png(source, (30 + index, 90, 150))
    staging_paths: list[Path] = []
    original_mkdtemp = daemon_client_module.tempfile.mkdtemp
    original_windows_stage = daemon_client_module._create_windows_visual_stage

    def captured_mkdtemp(*args, **kwargs) -> str:
        value = original_mkdtemp(*args, **kwargs)
        if Path(value).name.startswith(".vibecad_visual_"):
            staging_paths.append(Path(value))
        return value

    def captured_windows_stage(*args, **kwargs):
        value = original_windows_stage(*args, **kwargs)
        staging_paths.append(value[0])
        return value

    monkeypatch.setattr(daemon_client_module.tempfile, "mkdtemp", captured_mkdtemp)
    monkeypatch.setattr(
        daemon_client_module,
        "_create_windows_visual_stage",
        captured_windows_stage,
    )
    daemon = None
    client = None
    try:
        daemon = LocalKernelDaemon.start(data_root=base / "data")
        client = LocalAgentClient.connect(daemon.run_root)
        first = client.seal_visual_image_set_request(
            _request(count=4),
            source_paths=tuple(str(source) for source in sources),
        )
        second = client.seal_visual_image_set_request(
            _request(count=4),
            source_paths=tuple(str(source) for source in sources),
        )
        assert first == second
        assert set(first) == {
            "schema_version",
            "image_set_id",
            "image_set_manifest_sha256",
        }
        client.close()
        client = None
        daemon.close()
        daemon = None

        daemon = LocalKernelDaemon.start(data_root=base / "data")
        client = LocalAgentClient.connect(daemon.run_root)
        restarted = client.seal_visual_image_set_request(
            _request(count=4),
            source_paths=tuple(str(source) for source in sources),
        )
        assert restarted == first
        assert all(not path.exists() for path in staging_paths)
    finally:
        if client is not None:
            with contextlib.suppress(BaseException):
                client.close()
        if daemon is not None:
            daemon.close()
        shutil.rmtree(base, ignore_errors=True)
