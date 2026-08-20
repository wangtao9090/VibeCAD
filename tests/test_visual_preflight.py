from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest
from PIL import Image

import vibecad.application.visual_preflight as visual_preflight_module
import vibecad.visual.capture_quality as capture_quality_module
from vibecad.application.visual_preflight import assess_sealed_capture_quality
from vibecad.visual.capture_quality import (
    CaptureQualityDecision,
    CaptureQualityError,
    CaptureQualityErrorCode,
    CaptureQualityIssueCode,
    CaptureQualitySeverity,
)
from vibecad.visual.contracts import (
    CalibrationStatus,
    ImageMime,
    ProcessingAuthorization,
    ViewRole,
)
from vibecad.visual.inputs import (
    DescriptorSource,
    ImageIngress,
    SealImageSetRequest,
    VisualInputStore,
    VisualInputStoreError,
    VisualInputStoreErrorCode,
    bind_visual_input_locator,
)
from vibecad.workflow.lease import LeaseRootTrust, ResourceLeaseManager


def _parts(tmp_path: Path) -> tuple[Path, Path, VisualInputStore]:
    root = tmp_path / "visual_inputs"
    locks = tmp_path / "locks"
    root.mkdir(mode=0o700)
    locks.mkdir(mode=0o700)
    manager = ResourceLeaseManager(locks, trust=LeaseRootTrust.TRUSTED_LOCAL)
    identity = root.stat()
    return (
        root,
        locks,
        VisualInputStore(
            root=root,
            expected_root_identity=(identity.st_dev, identity.st_ino),
            lease_manager=manager,
        ),
    )


def _save(path: Path, *, blank: bool) -> None:
    if blank:
        image = Image.new("RGB", (96, 64), (80, 80, 80))
    else:
        image = Image.new("RGB", (96, 64))
        pixels = image.load()
        for y in range(image.height):
            for x in range(image.width):
                value = 235 if (x // 6 + y // 6) % 2 else 20
                pixels[x, y] = (value, value, value)
    image.save(path, format="PNG")
    os.chmod(path, 0o600)


def _seal(store: VisualInputStore, paths: tuple[Path, ...]):
    request = SealImageSetRequest(
        create_key="image_set_create_" + "7" * 32,
        inputs=tuple(
            ImageIngress(
                view_role=ViewRole.FRONT if index == 0 else ViewRole.TOP,
                calibration_status=CalibrationStatus.UNKNOWN,
                declared_mime=ImageMime.PNG,
            )
            for index in range(len(paths))
        ),
        unit="mm",
        dimension_hints=(),
        calibration_evidence=(),
        same_object=True,
        same_state=True,
        same_scale=True,
        processing_authorization=ProcessingAuthorization.CLOUD_PROVIDER,
    )
    descriptors: list[DescriptorSource] = []
    fds: list[int] = []
    try:
        for index, path in enumerate(paths):
            descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0))
            fds.append(descriptor)
            descriptors.append(
                DescriptorSource(
                    fd=descriptor,
                    locator=bind_visual_input_locator(
                        request,
                        index,
                        os.fstat(descriptor),
                    ),
                )
            )
        return store.seal(request, tuple(descriptors))
    finally:
        for descriptor in fds:
            os.close(descriptor)


def _tree_snapshot(root: Path) -> tuple[tuple[str, int, str], ...]:
    return tuple(
        (
            str(path.relative_to(root)),
            path.stat().st_mode,
            hashlib.sha256(path.read_bytes()).hexdigest(),
        )
        for path in sorted(root.rglob("*"))
        if path.is_file()
    )


def test_exact_report_is_read_only_and_restart_independent(tmp_path: Path) -> None:
    root, locks, store = _parts(tmp_path)
    front = tmp_path / "front.png"
    top = tmp_path / "top.png"
    _save(front, blank=False)
    _save(top, blank=True)
    sealed = _seal(store, (front, top))
    before = _tree_snapshot(root)

    first = assess_sealed_capture_quality(
        store=store,
        image_set_id=sealed.id,
        image_set_manifest_sha256=sealed.manifest_sha256,
    )
    identity = root.stat()
    restarted = VisualInputStore(
        root=root,
        expected_root_identity=(identity.st_dev, identity.st_ino),
        lease_manager=ResourceLeaseManager(locks, trust=LeaseRootTrust.TRUSTED_LOCAL),
    )
    second = assess_sealed_capture_quality(
        store=restarted,
        image_set_id=sealed.id,
        image_set_manifest_sha256=sealed.manifest_sha256,
    )

    assert first == second
    assert first.image_set_id == sealed.id
    assert first.image_set_manifest_sha256 == sealed.manifest_sha256
    assert first.quality.decision is CaptureQualityDecision.RECAPTURE_RECOMMENDED
    assert first.quality.readable_source_indices == (0,)
    assert any(
        finding.code is CaptureQualityIssueCode.NO_VISUAL_SIGNAL
        and finding.severity is CaptureQualitySeverity.UNREADABLE
        for finding in first.quality.findings
    )
    assert _tree_snapshot(root) == before


def test_wrong_generation_and_normalized_tamper_fail_closed(tmp_path: Path) -> None:
    root, _, store = _parts(tmp_path)
    source = tmp_path / "source.png"
    _save(source, blank=False)
    sealed = _seal(store, (source,))

    with pytest.raises(VisualInputStoreError) as wrong_generation:
        assess_sealed_capture_quality(
            store=store,
            image_set_id=sealed.id,
            image_set_manifest_sha256="0" * 64,
        )
    assert wrong_generation.value.code is VisualInputStoreErrorCode.CONFLICT

    normalized = root / sealed.id / f"{sealed.inputs[0].normalized.id}.png"
    normalized.write_bytes(normalized.read_bytes() + b"tamper")
    with pytest.raises(VisualInputStoreError) as tampered:
        assess_sealed_capture_quality(
            store=store,
            image_set_id=sealed.id,
            image_set_manifest_sha256=sealed.manifest_sha256,
        )
    assert tampered.value.code is VisualInputStoreErrorCode.INTEGRITY_FAILURE


def test_analyzer_receives_exact_sealed_identity_dimensions_and_digest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, _, store = _parts(tmp_path)
    source = tmp_path / "source.png"
    _save(source, blank=False)
    sealed = _seal(store, (source,))
    received = []
    analyzer = visual_preflight_module.assess_capture_quality

    def inspect(images):
        received.extend(images)
        return analyzer(images)

    monkeypatch.setattr(visual_preflight_module, "assess_capture_quality", inspect)
    assess_sealed_capture_quality(
        store=store,
        image_set_id=sealed.id,
        image_set_manifest_sha256=sealed.manifest_sha256,
    )

    assert len(received) == 1
    capture = received[0]
    normalized = sealed.inputs[0].normalized
    assert capture.visual_input_id == normalized.id
    assert (capture.width, capture.height) == (normalized.width, normalized.height)
    assert capture.sha256 == normalized.sha256
    assert hashlib.sha256(capture.data).hexdigest() == normalized.sha256


def test_bridge_stops_only_when_every_sealed_view_is_unreadable(tmp_path: Path) -> None:
    _, _, store = _parts(tmp_path)
    front = tmp_path / "front.png"
    top = tmp_path / "top.png"
    _save(front, blank=True)
    _save(top, blank=True)
    sealed = _seal(store, (front, top))

    report = assess_sealed_capture_quality(
        store=store,
        image_set_id=sealed.id,
        image_set_manifest_sha256=sealed.manifest_sha256,
    )

    assert report.quality.decision is CaptureQualityDecision.STOP
    assert report.quality.readable_source_indices == ()
    assert report.quality.findings
    assert all(
        finding.severity is CaptureQualitySeverity.UNREADABLE for finding in report.quality.findings
    )


def test_existing_capture_budget_is_enforced_after_exact_store_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, _, store = _parts(tmp_path)
    source = tmp_path / "source.png"
    _save(source, blank=False)
    sealed = _seal(store, (source,))
    monkeypatch.setattr(capture_quality_module, "MAX_IMAGE_SET_PHYSICAL_BYTES", 1)

    with pytest.raises(CaptureQualityError) as over_budget:
        assess_sealed_capture_quality(
            store=store,
            image_set_id=sealed.id,
            image_set_manifest_sha256=sealed.manifest_sha256,
        )
    assert over_budget.value.code is CaptureQualityErrorCode.BUDGET_EXCEEDED
