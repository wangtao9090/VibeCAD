from __future__ import annotations

import errno
import hashlib
import json
import os
import shutil
import struct
import zlib
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from PIL import Image, ImageCms, PngImagePlugin

import vibecad.visual.inputs as visual_inputs_module
from vibecad.visual import (
    MAX_IMAGE_SOURCE_BYTES,
    NORMALIZATION_PROFILE,
    SOURCE_JPEG_PROFILE,
    CalibrationEvidence,
    CalibrationKind,
    CalibrationStatus,
    DescriptorSource,
    DimensionHint,
    ImageIngress,
    ImageMime,
    ImageRef,
    ImageSet,
    ProcessingAuthorization,
    SealImageSetRequest,
    ViewRole,
    VisualContractError,
    VisualContractErrorCode,
    VisualInput,
    VisualInputStore,
    VisualInputStoreError,
    VisualInputStoreErrorCode,
    bind_visual_input_locator,
    decode_image_set,
    encode_image_set,
    image_set_identity,
    visual_input_identity,
)
from vibecad.workflow.lease import LeaseRootTrust, ResourceLeaseManager

CREATE_KEY = "image_set_create_0123456789abcdef0123456789abcdef"
OTHER_CREATE_KEY = "image_set_create_fedcba9876543210fedcba9876543210"


def _canonical(value: object) -> bytes:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _parts(tmp_path: Path) -> tuple[Path, VisualInputStore, ResourceLeaseManager]:
    root = tmp_path / "visual_inputs"
    locks = tmp_path / "locks"
    root.mkdir(mode=0o700)
    locks.mkdir(mode=0o700)
    os.chmod(root, 0o700)
    os.chmod(locks, 0o700)
    manager = ResourceLeaseManager(locks, trust=LeaseRootTrust.TRUSTED_LOCAL)
    info = root.stat()
    store = VisualInputStore(
        root=root,
        expected_root_identity=(info.st_dev, info.st_ino),
        lease_manager=manager,
    )
    return root, store, manager


def _request(
    *items: tuple[ImageMime, ViewRole],
    create_key: str = CREATE_KEY,
    processing_authorization: ProcessingAuthorization = ProcessingAuthorization.LOCAL_ONLY,
) -> SealImageSetRequest:
    return SealImageSetRequest(
        create_key=create_key,
        inputs=tuple(
            ImageIngress(
                view_role=role,
                calibration_status=CalibrationStatus.UNKNOWN,
                declared_mime=mime,
            )
            for mime, role in items
        ),
        unit="mm",
        dimension_hints=(),
        calibration_evidence=(),
        same_object=True,
        same_state=True,
        same_scale=True,
        processing_authorization=processing_authorization,
    )


def _save_jpeg(
    path: Path,
    *,
    size: tuple[int, int] = (64, 48),
    color: tuple[int, int, int] = (20, 80, 140),
    orientation: int | None = None,
) -> None:
    image = Image.new("RGB", size, color)
    exif = Image.Exif()
    if orientation is not None:
        exif[274] = orientation
        exif[270] = "must not survive normalization"
    image.save(path, format="JPEG", quality=91, exif=exif, comment=b"private-comment")
    os.chmod(path, 0o600)


def _save_png(
    path: Path,
    *,
    size: tuple[int, int] = (48, 64),
    color: tuple[int, int, int, int] = (150, 40, 20, 190),
    metadata: bool = False,
) -> None:
    image = Image.new("RGBA", size, color)
    pnginfo = None
    if metadata:
        pnginfo = PngImagePlugin.PngInfo()
        pnginfo.add_text("secret", "must not survive normalization")
    image.save(path, format="PNG", pnginfo=pnginfo)
    os.chmod(path, 0o600)


def _seal_paths(
    store: VisualInputStore,
    request: SealImageSetRequest,
    paths: tuple[Path, ...],
) -> ImageSet:
    descriptors: list[DescriptorSource] = []
    fds: list[int] = []
    try:
        for index, path in enumerate(paths):
            fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC)
            fds.append(fd)
            descriptors.append(
                DescriptorSource(
                    fd=fd,
                    locator=bind_visual_input_locator(request, index, os.fstat(fd)),
                )
            )
        return store.seal(request, tuple(descriptors))
    finally:
        for fd in fds:
            os.close(fd)


def _contract_image_set() -> ImageSet:
    image_set_id, create_key_digest = image_set_identity(CREATE_KEY)
    return ImageSet(
        id=image_set_id,
        create_key_digest=create_key_digest,
        inputs=(
            VisualInput(
                original=ImageRef(
                    id=visual_input_identity(CREATE_KEY, 0, "original"),
                    sha256="1" * 64,
                    size_bytes=32,
                    mime=ImageMime.JPEG,
                    width=8,
                    height=4,
                    profile="source-jpeg-v1",
                ),
                normalized=ImageRef(
                    id=visual_input_identity(CREATE_KEY, 0, "normalized"),
                    sha256="2" * 64,
                    size_bytes=48,
                    mime=ImageMime.PNG,
                    width=8,
                    height=4,
                    profile=NORMALIZATION_PROFILE,
                ),
                view_role=ViewRole.FRONT,
                calibration_status=CalibrationStatus.UNKNOWN,
            ),
        ),
        unit="mm",
        dimension_hints=(),
        calibration_evidence=(),
        same_object=True,
        same_state=True,
        same_scale=True,
        processing_authorization=ProcessingAuthorization.LOCAL_ONLY,
    )


def _png_header(width: int, height: int) -> bytes:
    def chunk(name: bytes, payload: bytes) -> bytes:
        body = name + payload
        return struct.pack(">I", len(payload)) + body + struct.pack(">I", zlib.crc32(body))

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IEND", b"")


def test_image_set_contract_is_canonical_and_rejects_identity_or_manifest_tamper() -> None:
    record = _contract_image_set()
    raw = encode_image_set(record)

    assert raw == _canonical(record.to_mapping())
    assert "image_set_id" in record.to_mapping()
    assert "visual_input_id" in record.to_mapping()["inputs"][0]["original"]
    assert decode_image_set(raw) == record
    assert image_set_identity(CREATE_KEY) == image_set_identity(CREATE_KEY)
    assert image_set_identity(CREATE_KEY) != image_set_identity(OTHER_CREATE_KEY)
    assert visual_input_identity(CREATE_KEY, 0, "original") != visual_input_identity(
        CREATE_KEY, 0, "normalized"
    )

    changed_body = json.loads(raw)
    changed_body["same_scale"] = False
    with pytest.raises(VisualContractError) as caught:
        decode_image_set(_canonical(changed_body))
    assert caught.value.code is VisualContractErrorCode.INTEGRITY_FAILURE

    invalid_normalized = ImageRef(
        id=record.inputs[0].normalized.id,
        sha256="2" * 64,
        size_bytes=48,
        mime=ImageMime.JPEG,
        width=8,
        height=4,
        profile="unsupported-profile",
    )
    with pytest.raises(VisualContractError) as caught:
        VisualInput(
            original=record.inputs[0].original,
            normalized=invalid_normalized,
            view_role=ViewRole.FRONT,
            calibration_status=CalibrationStatus.UNKNOWN,
        )
    assert caught.value.code is VisualContractErrorCode.INTEGRITY_FAILURE

    changed_identity = json.loads(raw)
    changed_identity["image_set_id"] = image_set_identity(OTHER_CREATE_KEY)[0]
    with pytest.raises(VisualContractError) as caught:
        decode_image_set(_canonical(changed_identity))
    assert caught.value.code is VisualContractErrorCode.INTEGRITY_FAILURE

    with pytest.raises(VisualContractError) as caught:
        decode_image_set(b" " + raw)
    assert caught.value.code is VisualContractErrorCode.INTEGRITY_FAILURE


def test_seal_get_and_reopen_preserve_jpeg_png_multiview_contract(tmp_path: Path) -> None:
    root, store, manager = _parts(tmp_path)
    front = tmp_path / "front.jpg"
    top = tmp_path / "top.png"
    _save_jpeg(front)
    _save_png(top, metadata=True)
    request = _request((ImageMime.JPEG, ViewRole.FRONT), (ImageMime.PNG, ViewRole.TOP))

    sealed = _seal_paths(store, request, (front, top))

    assert sealed.id == image_set_identity(CREATE_KEY)[0]
    assert tuple(item.view_role for item in sealed.inputs) == (ViewRole.FRONT, ViewRole.TOP)
    assert tuple(item.original.mime for item in sealed.inputs) == (ImageMime.JPEG, ImageMime.PNG)
    assert all(item.normalized.mime is ImageMime.PNG for item in sealed.inputs)
    assert store.get(sealed.id) == sealed
    assert decode_image_set(encode_image_set(sealed)) == sealed

    reopened = VisualInputStore(
        root=root,
        expected_root_identity=(root.stat().st_dev, root.stat().st_ino),
        lease_manager=manager,
    )
    assert reopened.get(sealed.id) == sealed
    sealed_dir = root / sealed.id
    assert sealed_dir.stat().st_mode & 0o777 == 0o700
    assert all(path.stat().st_mode & 0o777 == 0o600 for path in sealed_dir.iterdir())

    normalized_top = sealed_dir / f"{sealed.inputs[1].normalized.id}.png"
    with Image.open(normalized_top) as image:
        assert image.mode == "RGBA"
        assert "secret" not in image.info


def test_cloud_provider_reader_returns_exact_normalized_bytes_without_paths(tmp_path: Path) -> None:
    root, store, _ = _parts(tmp_path)
    source = tmp_path / "cloud.png"
    _save_png(source, metadata=True)
    request = _request(
        (ImageMime.PNG, ViewRole.FRONT),
        processing_authorization=ProcessingAuthorization.CLOUD_PROVIDER,
    )
    sealed = _seal_paths(store, request, (source,))

    record, images = store.read_provider_images_exact(sealed.id, sealed.manifest_sha256)

    assert record == sealed
    assert len(images) == 1
    assert hashlib.sha256(images[0]).hexdigest() == sealed.inputs[0].normalized.sha256
    assert images[0] == (root / sealed.id / f"{sealed.inputs[0].normalized.id}.png").read_bytes()
    with pytest.raises(VisualInputStoreError) as caught:
        store.read_provider_images_exact(sealed.id, "0" * 64)
    assert caught.value.code is VisualInputStoreErrorCode.CONFLICT

    local_source = tmp_path / "local.png"
    _save_png(local_source)
    local = _seal_paths(
        store,
        _request(
            (ImageMime.PNG, ViewRole.TOP),
            create_key=OTHER_CREATE_KEY,
        ),
        (local_source,),
    )
    with pytest.raises(VisualInputStoreError) as caught:
        store.read_provider_images_exact(local.id, local.manifest_sha256)
    assert caught.value.code is VisualInputStoreErrorCode.CONFLICT


def test_normalization_applies_exif_orientation_caps_edge_and_strips_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, store, _ = _parts(tmp_path)
    source = tmp_path / "oriented.jpg"
    _save_jpeg(source, size=(4100, 100), orientation=6)
    request = _request((ImageMime.JPEG, ViewRole.RIGHT))
    converted_sizes: list[tuple[int, int]] = []
    original_convert = Image.Image.convert

    def tracked_convert(image, *args, **kwargs):
        converted_sizes.append(image.size)
        return original_convert(image, *args, **kwargs)

    monkeypatch.setattr(Image.Image, "convert", tracked_convert)

    sealed = _seal_paths(store, request, (source,))

    item = sealed.inputs[0]
    assert (item.original.width, item.original.height) == (4100, 100)
    assert (item.normalized.width, item.normalized.height) == (100, 4096)
    assert converted_sizes
    assert all(max(size) <= 4096 for size in converted_sizes)
    normalized = root / sealed.id / f"{item.normalized.id}.png"
    with Image.open(normalized) as image:
        assert image.size == (100, 4096)
        assert image.mode == "RGB"
        assert len(image.getexif()) == 0
        assert "exif" not in image.info
        assert "icc_profile" not in image.info
        assert "comment" not in image.info


def test_same_create_key_is_idempotent_but_conflicts_with_different_content(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, store, _ = _parts(tmp_path)
    first_path = tmp_path / "first.png"
    changed_path = tmp_path / "changed.png"
    _save_png(first_path, color=(20, 40, 80, 255))
    _save_png(changed_path, color=(80, 40, 20, 255))
    request = _request((ImageMime.PNG, ViewRole.ISOMETRIC))

    first = _seal_paths(store, request, (first_path,))

    def forbidden_normalize(*args, **kwargs):
        raise AssertionError("idempotent replay must not regenerate derivatives")

    monkeypatch.setattr(VisualInputStore, "_normalize", forbidden_normalize)
    replay = _seal_paths(store, request, (first_path,))

    assert replay == first
    with pytest.raises(VisualInputStoreError) as caught:
        _seal_paths(store, request, (changed_path,))
    assert caught.value.code is VisualInputStoreErrorCode.CONFLICT
    assert tuple(path.name for path in root.iterdir()) == (first.id,)


def test_explicit_scale_requires_structured_calibration_evidence() -> None:
    ingress = ImageIngress(
        view_role=ViewRole.FRONT,
        calibration_status=CalibrationStatus.EXPLICIT_SCALE,
        declared_mime=ImageMime.JPEG,
    )
    common = {
        "create_key": CREATE_KEY,
        "inputs": (ingress,),
        "unit": "mm",
        "dimension_hints": (),
        "same_object": True,
        "same_state": True,
        "same_scale": True,
    }
    with pytest.raises(VisualInputStoreError) as caught:
        SealImageSetRequest(calibration_evidence=(), **common)
    assert caught.value.code is VisualInputStoreErrorCode.INVALID_INPUT

    evidence = CalibrationEvidence(
        source_index=0,
        kind=CalibrationKind.SCALE,
        reference="engraved 10 mm scale bar",
        scale_mm_per_pixel=0.05,
        focal_length_px=None,
        principal_x_px=None,
        principal_y_px=None,
    )
    request = SealImageSetRequest(calibration_evidence=(evidence,), **common)

    assert request.calibration_evidence == (evidence,)
    assert request.to_mapping()["calibration_evidence"] == [evidence.to_mapping()]
    assert SOURCE_JPEG_PROFILE == "source-jpeg-v1"


def test_hostile_contract_scalars_return_bounded_domain_errors() -> None:
    with pytest.raises(VisualContractError) as caught:
        DimensionHint(name="width", value_mm=10**1000, source_index=0)
    assert caught.value.code is VisualContractErrorCode.INVALID_INPUT

    with pytest.raises(VisualContractError) as caught:
        CalibrationEvidence(
            source_index=0,
            kind=CalibrationKind.SCALE,
            reference="scale",
            scale_mm_per_pixel=10**1000,
            focal_length_px=None,
            principal_x_px=None,
            principal_y_px=None,
        )
    assert caught.value.code is VisualContractErrorCode.INVALID_INPUT

    with pytest.raises(VisualContractError) as caught:
        visual_input_identity(CREATE_KEY, 0, [])
    assert caught.value.code is VisualContractErrorCode.INVALID_INPUT

    ingress = ImageIngress(
        view_role=ViewRole.UNKNOWN,
        calibration_status=CalibrationStatus.UNKNOWN,
        declared_mime=ImageMime.PNG,
    )
    with pytest.raises(VisualInputStoreError) as caught:
        SealImageSetRequest(
            create_key=CREATE_KEY,
            inputs=(ingress,),
            unit=[],
            dimension_hints=(),
            calibration_evidence=(),
            same_object=True,
            same_state=True,
            same_scale=False,
        )
    assert caught.value.code is VisualInputStoreErrorCode.INVALID_INPUT


@pytest.mark.parametrize(
    ("profile_path", "mode", "mime"),
    (
        pytest.param(
            Path("/System/Library/ColorSync/Profiles/Generic CMYK Profile.icc"),
            "CMYK",
            ImageMime.JPEG,
            id="cmyk-jpeg",
        ),
        pytest.param(
            Path("/System/Library/ColorSync/Profiles/Generic Gray Profile.icc"),
            "L",
            ImageMime.PNG,
            id="gray-png",
        ),
    ),
)
def test_valid_non_rgb_icc_sources_normalize_to_srgb(
    tmp_path: Path,
    profile_path: Path,
    mode: str,
    mime: ImageMime,
) -> None:
    if not profile_path.is_file():
        pytest.skip("portable CMYK/Gray ICC creation is unavailable on this platform")
    root, store, _ = _parts(tmp_path)
    source = tmp_path / ("source.jpg" if mime is ImageMime.JPEG else "source.png")
    profile = ImageCms.getOpenProfile(str(profile_path)).tobytes()
    color = (10, 30, 60, 5) if mode == "CMYK" else 96
    Image.new(mode, (24, 16), color).save(
        source,
        format="JPEG" if mime is ImageMime.JPEG else "PNG",
        icc_profile=profile,
    )
    os.chmod(source, 0o600)
    request = _request((mime, ViewRole.FRONT))

    sealed = _seal_paths(store, request, (source,))

    normalized = root / sealed.id / f"{sealed.inputs[0].normalized.id}.png"
    with Image.open(normalized) as image:
        assert image.mode == "RGB"
        assert image.info.get("icc_profile") is None


def test_concurrent_same_create_key_seals_converge_without_temporary_entries(
    tmp_path: Path,
) -> None:
    root, store, _ = _parts(tmp_path)
    source = tmp_path / "source.png"
    _save_png(source)
    request = _request((ImageMime.PNG, ViewRole.ISOMETRIC))

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = tuple(executor.submit(_seal_paths, store, request, (source,)) for _ in range(2))
        results = tuple(future.result(timeout=5) for future in futures)

    assert results[0] == results[1]
    assert tuple(path.name for path in root.iterdir()) == (results[0].id,)
    assert not any(path.name.startswith(".stage_") for path in root.iterdir())


def test_pre_publish_failure_cleans_stage_and_retry_succeeds(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, store, _ = _parts(tmp_path)
    source = tmp_path / "source.png"
    _save_png(source)
    request = _request((ImageMime.PNG, ViewRole.FRONT))
    rename = visual_inputs_module._rename_directory_noreplace

    def fail_publish(parent_fd: int, source_name: str, destination: str) -> None:
        raise OSError(errno.EIO, "injected pre-publish failure")

    monkeypatch.setattr(visual_inputs_module, "_rename_directory_noreplace", fail_publish)
    with pytest.raises(VisualInputStoreError) as caught:
        _seal_paths(store, request, (source,))

    assert caught.value.code is VisualInputStoreErrorCode.STORE_FAILURE
    assert list(root.iterdir()) == []

    monkeypatch.setattr(visual_inputs_module, "_rename_directory_noreplace", rename)
    sealed = _seal_paths(store, request, (source,))
    assert tuple(path.name for path in root.iterdir()) == (sealed.id,)
    assert store.get(sealed.id) == sealed


def test_locator_is_bound_to_exact_request_and_file_identity(tmp_path: Path) -> None:
    root, store, _ = _parts(tmp_path)
    source = tmp_path / "source.png"
    _save_png(source)
    request = _request((ImageMime.PNG, ViewRole.FRONT))
    fd = os.open(source, os.O_RDONLY | os.O_CLOEXEC)
    try:
        locator = bind_visual_input_locator(request, 0, os.fstat(fd))
        locator["ino"] = int(locator["ino"]) + 1
        descriptor = DescriptorSource(fd=fd, locator=locator)
        with pytest.raises(VisualInputStoreError) as caught:
            store.seal(request, (descriptor,))
    finally:
        os.close(fd)

    assert caught.value.code is VisualInputStoreErrorCode.INVALID_INPUT
    assert list(root.iterdir()) == []


def test_hardlinked_and_oversized_sources_are_rejected_before_ingress(tmp_path: Path) -> None:
    source = tmp_path / "source.png"
    linked = tmp_path / "linked.png"
    oversized = tmp_path / "oversized.jpg"
    _save_png(source)
    os.link(source, linked)
    request = _request((ImageMime.PNG, ViewRole.FRONT))

    with pytest.raises(VisualInputStoreError) as caught:
        bind_visual_input_locator(request, 0, source.stat())
    assert caught.value.code is VisualInputStoreErrorCode.INVALID_INPUT

    with oversized.open("wb") as stream:
        stream.write(b"\xff\xd8\xff")
        stream.truncate(MAX_IMAGE_SOURCE_BYTES + 1)
    os.chmod(oversized, 0o600)
    jpeg_request = _request((ImageMime.JPEG, ViewRole.FRONT))
    with pytest.raises(VisualInputStoreError) as caught:
        bind_visual_input_locator(jpeg_request, 0, oversized.stat())
    assert caught.value.code is VisualInputStoreErrorCode.INVALID_INPUT


def test_sixteen_image_ceiling_is_enforced_by_request_contract() -> None:
    ingress = ImageIngress(
        view_role=ViewRole.UNKNOWN,
        calibration_status=CalibrationStatus.UNKNOWN,
        declared_mime=ImageMime.PNG,
    )

    with pytest.raises(VisualInputStoreError) as caught:
        SealImageSetRequest(
            create_key=CREATE_KEY,
            inputs=(ingress,) * 17,
            unit=None,
            dimension_hints=(),
            calibration_evidence=(),
            same_object=True,
            same_state=True,
            same_scale=False,
        )

    assert caught.value.code is VisualInputStoreErrorCode.INVALID_INPUT


@pytest.mark.parametrize("kind", ["bad_magic", "truncated", "animated", "pixel_budget"])
def test_invalid_or_over_budget_image_payloads_fail_without_publishing(
    tmp_path: Path,
    kind: str,
) -> None:
    root, store, _ = _parts(tmp_path)
    source = tmp_path / "source"
    if kind == "bad_magic":
        _save_png(source)
        mime = ImageMime.JPEG
    elif kind == "truncated":
        source.write_bytes(b"\xff\xd8\xff\xe0\x00\x10JFIF")
        os.chmod(source, 0o600)
        mime = ImageMime.JPEG
    elif kind == "animated":
        first = Image.new("RGBA", (8, 8), (255, 0, 0, 255))
        second = Image.new("RGBA", (8, 8), (0, 0, 255, 255))
        first.save(
            source,
            format="PNG",
            save_all=True,
            append_images=[second],
            duration=25,
            loop=0,
        )
        os.chmod(source, 0o600)
        mime = ImageMime.PNG
    else:
        source.write_bytes(_png_header(7000, 6000))
        os.chmod(source, 0o600)
        mime = ImageMime.PNG
    request = _request((mime, ViewRole.UNKNOWN))

    with pytest.raises(VisualInputStoreError) as caught:
        _seal_paths(store, request, (source,))

    expected = (
        VisualInputStoreErrorCode.BUDGET_EXCEEDED
        if kind == "pixel_budget"
        else VisualInputStoreErrorCode.INVALID_INPUT
    )
    assert caught.value.code is expected
    assert list(root.iterdir()) == []


@pytest.mark.parametrize("target", ["manifest", "normalized"])
def test_sealed_tamper_fails_closed_without_rewriting_evidence(
    tmp_path: Path,
    target: str,
) -> None:
    root, store, _ = _parts(tmp_path)
    source = tmp_path / "source.png"
    _save_png(source)
    request = _request((ImageMime.PNG, ViewRole.FRONT))
    sealed = _seal_paths(store, request, (source,))
    sealed_dir = root / sealed.id

    if target == "manifest":
        path = sealed_dir / "manifest.json"
        body = json.loads(path.read_bytes())
        body["same_state"] = False
        path.write_bytes(_canonical(body))
    else:
        path = sealed_dir / f"{sealed.inputs[0].normalized.id}.png"
        raw = bytearray(path.read_bytes())
        raw[-1] ^= 1
        path.write_bytes(raw)
    os.chmod(path, 0o600)
    evidence = path.read_bytes()

    with pytest.raises(VisualInputStoreError) as caught:
        store.get(sealed.id)

    assert caught.value.code is VisualInputStoreErrorCode.INTEGRITY_FAILURE
    assert path.read_bytes() == evidence


def test_finalize_retires_id_without_manifest_and_permanently_blocks_reuse(
    tmp_path: Path,
) -> None:
    root, store, manager = _parts(tmp_path)
    source = tmp_path / "source.png"
    _save_png(source)
    request = _request((ImageMime.PNG, ViewRole.FRONT))
    sealed = _seal_paths(store, request, (source,))
    digest = sealed.manifest_sha256

    assert store.delete_exact(sealed.id, digest) is None
    marker = root / f".deleted_{sealed.id.removeprefix('image_set_')}.json"
    assert marker.is_file()
    assert marker.stat().st_mode & 0o777 == 0o600
    with pytest.raises(VisualInputStoreError) as caught:
        store.get(sealed.id)
    assert caught.value.code is VisualInputStoreErrorCode.NOT_FOUND

    reopened = VisualInputStore(
        root=root,
        expected_root_identity=(root.stat().st_dev, root.stat().st_ino),
        lease_manager=manager,
    )
    assert reopened.delete_exact(sealed.id, digest) is None
    with pytest.raises(VisualInputStoreError) as caught:
        _seal_paths(reopened, request, (source,))
    assert caught.value.code is VisualInputStoreErrorCode.CONFLICT
    assert reopened.finalize_delete_exact(sealed.id, digest) is None
    assert not marker.exists()
    retired = root / f".retired_{sealed.id.removeprefix('image_set_')}.json"
    retired_body = json.loads(retired.read_bytes())
    assert set(retired_body) == {"schema_version", "image_set_id", "retired_sha256"}
    assert retired_body["image_set_id"] == sealed.id
    assert digest not in retired.read_text()
    assert all(item.original.sha256 not in retired.read_text() for item in sealed.inputs)
    assert reopened.finalize_delete_exact(sealed.id, digest) is None
    with pytest.raises(VisualInputStoreError) as caught:
        _seal_paths(reopened, request, (source,))
    assert caught.value.code is VisualInputStoreErrorCode.CONFLICT
    assert reopened.delete_exact(sealed.id, "f" * 64) is None


def test_delete_exact_rejects_wrong_digest_but_missing_cleanup_is_idempotent(
    tmp_path: Path,
) -> None:
    root, store, _ = _parts(tmp_path)
    source = tmp_path / "source.png"
    _save_png(source)
    request = _request((ImageMime.PNG, ViewRole.FRONT))
    sealed = _seal_paths(store, request, (source,))

    with pytest.raises(VisualInputStoreError) as caught:
        store.delete_exact(sealed.id, "0" * 64)
    assert caught.value.code is VisualInputStoreErrorCode.CONFLICT
    assert store.get(sealed.id) == sealed

    missing_id = image_set_identity(OTHER_CREATE_KEY)[0]
    assert store.delete_exact(missing_id, "1" * 64) is None
    assert store.finalize_delete_exact(missing_id, "1" * 64) is None
    assert store.delete_exact(missing_id, "f" * 64) is None
    assert {path.name for path in root.iterdir()} == {
        sealed.id,
        f".retired_{missing_id.removeprefix('image_set_')}.json",
    }


def test_finalize_delete_exact_rejects_wrong_digest_and_never_touches_reappeared_target(
    tmp_path: Path,
) -> None:
    root, store, _ = _parts(tmp_path)
    source = tmp_path / "source.png"
    backup = tmp_path / "sealed-backup"
    _save_png(source)
    request = _request((ImageMime.PNG, ViewRole.FRONT))
    sealed = _seal_paths(store, request, (source,))
    shutil.copytree(root / sealed.id, backup)
    store.delete_exact(sealed.id, sealed.manifest_sha256)
    marker = root / f".deleted_{sealed.id.removeprefix('image_set_')}.json"

    with pytest.raises(VisualInputStoreError) as caught:
        store.finalize_delete_exact(sealed.id, "0" * 64)
    assert caught.value.code is VisualInputStoreErrorCode.CONFLICT
    assert marker.is_file()

    assert store.finalize_delete_exact(sealed.id, sealed.manifest_sha256) is None
    retired = root / f".retired_{sealed.id.removeprefix('image_set_')}.json"
    assert retired.is_file()
    assert not marker.exists()
    os.rename(backup, root / sealed.id)
    with pytest.raises(VisualInputStoreError) as caught:
        store.finalize_delete_exact(sealed.id, sealed.manifest_sha256)
    assert caught.value.code is VisualInputStoreErrorCode.INTEGRITY_FAILURE
    assert store.get(sealed.id) == sealed
    assert retired.is_file()


def test_retired_ids_share_the_reconstruction_lifetime_budget(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, store, _ = _parts(tmp_path)
    source = tmp_path / "source.png"
    _save_png(source)
    monkeypatch.setattr(visual_inputs_module, "MAX_IMAGE_SETS", 2)

    for index in range(2):
        request = _request(
            (ImageMime.PNG, ViewRole.FRONT),
            create_key=f"image_set_create_{index + 1:032x}",
        )
        sealed = _seal_paths(store, request, (source,))
        assert store.delete_exact(sealed.id, sealed.manifest_sha256) is None
        assert store.finalize_delete_exact(sealed.id, sealed.manifest_sha256) is None
    request = _request(
        (ImageMime.PNG, ViewRole.FRONT),
        create_key=f"image_set_create_{3:032x}",
    )
    with pytest.raises(VisualInputStoreError) as caught:
        _seal_paths(store, request, (source,))
    assert caught.value.code is VisualInputStoreErrorCode.BUDGET_EXCEEDED
    assert len(list(root.iterdir())) == 2


def test_missing_finalize_cannot_overrun_the_reconstruction_lifetime_budget(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, store, _ = _parts(tmp_path)
    monkeypatch.setattr(visual_inputs_module, "MAX_IMAGE_SETS", 2)
    image_set_ids = tuple(
        image_set_identity(f"image_set_create_{index:032x}")[0] for index in range(1, 4)
    )

    for index, image_set_id in enumerate(image_set_ids[:2], start=1):
        assert store.finalize_delete_exact(image_set_id, f"{index}" * 64) is None

    with pytest.raises(VisualInputStoreError) as caught:
        store.finalize_delete_exact(image_set_ids[2], "3" * 64)

    assert caught.value.code is VisualInputStoreErrorCode.BUDGET_EXCEEDED
    assert {path.name for path in root.iterdir()} == {
        f".retired_{image_set_id.removeprefix('image_set_')}.json"
        for image_set_id in image_set_ids[:2]
    }


def test_delete_exact_fails_closed_on_tombstone_tamper(tmp_path: Path) -> None:
    root, store, _ = _parts(tmp_path)
    source = tmp_path / "source.png"
    _save_png(source)
    request = _request((ImageMime.PNG, ViewRole.FRONT))
    sealed = _seal_paths(store, request, (source,))
    digest = sealed.manifest_sha256
    store.delete_exact(sealed.id, digest)
    marker = root / f".deleted_{sealed.id.removeprefix('image_set_')}.json"
    body = json.loads(marker.read_bytes())
    body["manifest_sha256"] = "f" * 64
    marker.write_bytes(_canonical(body))
    os.chmod(marker, 0o600)

    with pytest.raises(VisualInputStoreError) as caught:
        store.delete_exact(sealed.id, digest)

    assert caught.value.code is VisualInputStoreErrorCode.INTEGRITY_FAILURE
    assert marker.read_bytes() == _canonical(body)


@pytest.mark.parametrize("fault", ["before_rename", "after_rename", "partial_cleanup"])
def test_delete_exact_recovers_after_durable_marker_fault(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fault: str,
) -> None:
    root, store, manager = _parts(tmp_path)
    source = tmp_path / "source.png"
    _save_png(source)
    request = _request((ImageMime.PNG, ViewRole.FRONT))
    sealed = _seal_paths(store, request, (source,))
    digest = sealed.manifest_sha256

    if fault == "before_rename":
        original = visual_inputs_module._rename_directory_noreplace

        def fail_directory_publish(parent_fd: int, source_name: str, destination: str) -> None:
            if source_name == sealed.id:
                os.mkdir(destination, 0o700, dir_fd=parent_fd)
            original(parent_fd, source_name, destination)

        monkeypatch.setattr(
            visual_inputs_module,
            "_rename_directory_noreplace",
            fail_directory_publish,
        )
    elif fault == "after_rename":
        original = VisualInputStore._remove_sealed_directory

        def fail_cleanup(*args, **kwargs):
            raise OSError(errno.EIO, "injected cleanup failure")

        monkeypatch.setattr(VisualInputStore, "_remove_sealed_directory", fail_cleanup)
    else:
        original = visual_inputs_module.os.unlink
        calls = 0

        def fail_second_unlink(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError(errno.EIO, "injected partial cleanup failure")
            return original(*args, **kwargs)

        monkeypatch.setattr(visual_inputs_module.os, "unlink", fail_second_unlink)

    with pytest.raises(VisualInputStoreError) as caught:
        store.delete_exact(sealed.id, digest)
    assert caught.value.code is VisualInputStoreErrorCode.RECOVERY_REQUIRED

    if fault == "before_rename":
        assert (root / sealed.id).is_dir()
        assert (root / f".delete_{sealed.id.removeprefix('image_set_')}").is_dir()
        monkeypatch.setattr(visual_inputs_module, "_rename_directory_noreplace", original)
    elif fault == "after_rename":
        monkeypatch.setattr(VisualInputStore, "_remove_sealed_directory", original)
    else:
        monkeypatch.setattr(visual_inputs_module.os, "unlink", original)
    reopened = VisualInputStore(
        root=root,
        expected_root_identity=(root.stat().st_dev, root.stat().st_ino),
        lease_manager=manager,
    )
    assert reopened.delete_exact(sealed.id, digest) is None
    assert tuple(path.name for path in root.iterdir()) == (
        f".deleted_{sealed.id.removeprefix('image_set_')}.json",
    )


@pytest.mark.parametrize("fault", ["partial_write", "pre_publish"])
def test_delete_marker_temporary_is_safely_recovered_before_publish(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fault: str,
) -> None:
    root, store, manager = _parts(tmp_path)
    source = tmp_path / "source.png"
    _save_png(source)
    request = _request((ImageMime.PNG, ViewRole.FRONT))
    sealed = _seal_paths(store, request, (source,))
    digest = sealed.manifest_sha256
    temporary = root / f".delete_marker_{sealed.id.removeprefix('image_set_')}.tmp"
    marker = root / f".deleted_{sealed.id.removeprefix('image_set_')}.json"

    if fault == "partial_write":
        original = visual_inputs_module._write_all

        def partial_write(fd: int, raw: bytes) -> None:
            os.write(fd, raw[: len(raw) // 2])
            raise OSError(errno.EIO, "injected partial marker write")

        monkeypatch.setattr(visual_inputs_module, "_write_all", partial_write)
    else:
        original = visual_inputs_module._rename_directory_noreplace

        def fail_marker_publish(parent_fd: int, source_name: str, destination: str) -> None:
            if source_name.startswith(".delete_marker_"):
                raise OSError(errno.EIO, "injected pre-publish failure")
            original(parent_fd, source_name, destination)

        monkeypatch.setattr(
            visual_inputs_module,
            "_rename_directory_noreplace",
            fail_marker_publish,
        )

    with pytest.raises(VisualInputStoreError) as caught:
        store.delete_exact(sealed.id, digest)
    assert caught.value.code is VisualInputStoreErrorCode.RECOVERY_REQUIRED
    assert temporary.is_file()
    assert not marker.exists()
    assert store.get(sealed.id) == sealed

    if fault == "partial_write":
        monkeypatch.setattr(visual_inputs_module, "_write_all", original)
    else:
        monkeypatch.setattr(visual_inputs_module, "_rename_directory_noreplace", original)
    reopened = VisualInputStore(
        root=root,
        expected_root_identity=(root.stat().st_dev, root.stat().st_ino),
        lease_manager=manager,
    )
    assert reopened.delete_exact(sealed.id, digest) is None
    assert not temporary.exists()
    assert marker.is_file()


def test_delete_marker_publish_does_not_overwrite_racing_destination(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, store, _ = _parts(tmp_path)
    source = tmp_path / "source.png"
    _save_png(source)
    request = _request((ImageMime.PNG, ViewRole.FRONT))
    sealed = _seal_paths(store, request, (source,))
    suffix = sealed.id.removeprefix("image_set_")
    marker = root / f".deleted_{suffix}.json"
    temporary = root / f".delete_marker_{suffix}.tmp"
    competing_digest = "f" * 64
    competing_raw = visual_inputs_module._delete_marker_raw(sealed.id, competing_digest)
    original = visual_inputs_module._rename_directory_noreplace

    def race_publish(parent_fd: int, source_name: str, destination: str) -> None:
        if source_name == temporary.name:
            marker.write_bytes(competing_raw)
            os.chmod(marker, 0o600)
        original(parent_fd, source_name, destination)

    monkeypatch.setattr(
        visual_inputs_module,
        "_rename_directory_noreplace",
        race_publish,
    )

    with pytest.raises(VisualInputStoreError) as caught:
        store.delete_exact(sealed.id, sealed.manifest_sha256)

    assert caught.value.code is VisualInputStoreErrorCode.CONFLICT
    assert marker.read_bytes() == competing_raw
    assert not temporary.exists()
    assert store.get(sealed.id) == sealed


@pytest.mark.parametrize("fault", ["retired_pre_publish", "before_exact_cleanup"])
def test_finalize_delete_exact_recovers_retired_publication_faults(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fault: str,
) -> None:
    root, store, manager = _parts(tmp_path)
    source = tmp_path / "source.png"
    _save_png(source)
    sealed = _seal_paths(store, _request((ImageMime.PNG, ViewRole.FRONT)), (source,))
    store.delete_exact(sealed.id, sealed.manifest_sha256)
    suffix = sealed.id.removeprefix("image_set_")
    exact = root / f".deleted_{suffix}.json"
    retired = root / f".retired_{suffix}.json"
    retired_temporary = root / f".retire_marker_{suffix}.tmp"

    if fault == "retired_pre_publish":
        original = visual_inputs_module._rename_directory_noreplace

        def fail_retired_publish(parent_fd: int, source_name: str, destination: str) -> None:
            if source_name == retired_temporary.name:
                raise OSError(errno.EIO, "injected retired publish failure")
            original(parent_fd, source_name, destination)

        monkeypatch.setattr(
            visual_inputs_module,
            "_rename_directory_noreplace",
            fail_retired_publish,
        )
    else:
        original = VisualInputStore._remove_delete_marker

        def fail_exact_cleanup(*args, **kwargs):
            raise OSError(errno.EIO, "injected exact marker cleanup failure")

        monkeypatch.setattr(VisualInputStore, "_remove_delete_marker", fail_exact_cleanup)

    with pytest.raises(VisualInputStoreError) as caught:
        store.finalize_delete_exact(sealed.id, sealed.manifest_sha256)
    assert caught.value.code is VisualInputStoreErrorCode.RECOVERY_REQUIRED
    assert exact.is_file()
    if fault == "retired_pre_publish":
        assert retired_temporary.is_file()
        assert not retired.exists()
        monkeypatch.setattr(visual_inputs_module, "_rename_directory_noreplace", original)
    else:
        assert retired.is_file()
        monkeypatch.setattr(VisualInputStore, "_remove_delete_marker", original)

    reopened = VisualInputStore(
        root=root,
        expected_root_identity=(root.stat().st_dev, root.stat().st_ino),
        lease_manager=manager,
    )
    assert reopened.finalize_delete_exact(sealed.id, sealed.manifest_sha256) is None
    assert retired.is_file()
    assert not exact.exists()
    assert not retired_temporary.exists()


def test_retired_publish_does_not_overwrite_racing_destination(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, store, _ = _parts(tmp_path)
    source = tmp_path / "source.png"
    _save_png(source)
    sealed = _seal_paths(store, _request((ImageMime.PNG, ViewRole.FRONT)), (source,))
    store.delete_exact(sealed.id, sealed.manifest_sha256)
    suffix = sealed.id.removeprefix("image_set_")
    retired = root / f".retired_{suffix}.json"
    temporary = root / f".retire_marker_{suffix}.tmp"
    racing_raw = visual_inputs_module._retired_marker_raw(sealed.id)
    original = visual_inputs_module._rename_directory_noreplace
    racing_inode: int | None = None

    def race_publish(parent_fd: int, source_name: str, destination: str) -> None:
        nonlocal racing_inode
        if source_name == temporary.name:
            retired.write_bytes(racing_raw)
            os.chmod(retired, 0o600)
            racing_inode = retired.stat().st_ino
        original(parent_fd, source_name, destination)

    monkeypatch.setattr(
        visual_inputs_module,
        "_rename_directory_noreplace",
        race_publish,
    )

    assert store.finalize_delete_exact(sealed.id, sealed.manifest_sha256) is None
    assert retired.read_bytes() == racing_raw
    assert retired.stat().st_ino == racing_inode
    assert not temporary.exists()


@pytest.mark.parametrize("target_state", ["final_marker_present", "source_missing"])
def test_delete_marker_temporary_is_only_recovered_for_an_intact_unmarked_source(
    tmp_path: Path,
    target_state: str,
) -> None:
    root, store, _ = _parts(tmp_path)
    source = tmp_path / "source.png"
    _save_png(source)
    sealed = _seal_paths(store, _request((ImageMime.PNG, ViewRole.FRONT)), (source,))
    suffix = sealed.id.removeprefix("image_set_")
    temporary = root / f".delete_marker_{suffix}.tmp"
    temporary.write_bytes(b"interrupted marker")
    os.chmod(temporary, 0o600)

    if target_state == "final_marker_present":
        marker = root / f".deleted_{suffix}.json"
        marker.write_bytes(
            visual_inputs_module._delete_marker_raw(sealed.id, sealed.manifest_sha256)
        )
        os.chmod(marker, 0o600)
    else:
        directory = root / sealed.id
        for child in directory.iterdir():
            child.unlink()
        directory.rmdir()

    with pytest.raises(VisualInputStoreError) as caught:
        store.delete_exact(sealed.id, sealed.manifest_sha256)

    assert caught.value.code is VisualInputStoreErrorCode.INTEGRITY_FAILURE
    assert temporary.read_bytes() == b"interrupted marker"
