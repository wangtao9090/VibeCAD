"""Trusted attachment ingress and application composition tests."""

from __future__ import annotations

import hashlib
import io
import json
import os
import stat
from pathlib import Path

import pytest
from PIL import Image

import vibecad.application.reviewed_input_ingress as ingress_module
from vibecad.application.agent import AgentApplication
from vibecad.application.reviewed_input_ingress import (
    REVIEWED_INPUT_CATALOG_DIRECTORY,
    REVIEWED_INPUT_CATALOG_MANIFEST,
    ReviewedInputCatalogStore,
    ReviewedInputIngressError,
    ReviewedInputIngressErrorCode,
    ReviewedInputKind,
    TrustedReviewedInputBytes,
    TrustedReviewedInputDescriptor,
    TrustedReviewedInputFileDescriptor,
)
from vibecad.execution.freecad_reviewed_artifact_host import REVIEWED_ARTIFACT_MANIFEST_NAME
from vibecad.workflow.contracts import AcceptanceSpec, ModelCommand, ModelProgram, ValueSource
from vibecad.workflow.program import validate_model_program
from vibecad.workflow.state import ReasoningOwner, ReviewPolicy

_TASK_ID = "task_0123456789abcdef0123456789abcdef"
_PROJECT_ID = "project_0123456789abcdef0123456789abcdef"
_BASE_REVISION = "revision_0123456789abcdef0123456789abcdef"
_RUN_ID = "run_0123456789abcdef0123456789abcdef"
_STEP = b"ISO-10303-21;\nHEADER;\nENDSEC;\nDATA;\nENDSEC;\nEND-ISO-10303-21;\n"
_BREP = b"DBRep_DrawableShape\nCASCADE Topology V1, (c) Open Cascade\n"
_IGES = (b" " * 72 + b"S0000001") + (b" " * 72 + b"T0000001")
_PNG = (
    Path(__file__).parent / "fixtures" / "guided_photo_v1" / "images" / "calibration_block.png"
).read_bytes()


def _jpeg() -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (2, 2), (10, 20, 30)).save(buffer, format="JPEG")
    return buffer.getvalue()


_JPEG = _jpeg()


def _private(path: Path) -> Path:
    path.mkdir(mode=0o700, parents=True)
    path.chmod(0o700)
    return path


def _descriptor(kind: ReviewedInputKind, payload: bytes) -> TrustedReviewedInputDescriptor:
    return TrustedReviewedInputDescriptor(
        kind=kind,
        content_sha256=hashlib.sha256(payload).hexdigest(),
        size_bytes=len(payload),
    )


def _program(task_id: str = _TASK_ID, base_revision: str = _BASE_REVISION):
    return validate_model_program(
        ModelProgram(
            task_id=task_id,
            base_revision=base_revision,
            operations=(
                ModelCommand(
                    id="inspect",
                    op="inspect_model",
                    target={},
                    args={},
                    source=ValueSource.MODEL,
                ),
            ),
            acceptance=AcceptanceSpec(id="accept_reviewed_inputs", criteria=()),
        )
    )


def _store(root: Path) -> ReviewedInputCatalogStore:
    value = root.lstat()
    return ReviewedInputCatalogStore(
        application_root=root,
        expected_root_identity=(value.st_dev, value.st_ino),
    )


@pytest.mark.parametrize(
    ("kind", "payload", "media_type", "role", "schema", "family", "operation"),
    (
        (
            ReviewedInputKind.BREP,
            _BREP,
            "model/vnd.opencascade.brep",
            "role_part_file_import_artifact",
            "schema_part_brep_artifact_v1",
            "freecad_part_file_import",
            "brep",
        ),
        (
            ReviewedInputKind.IGES,
            _IGES,
            "model/iges",
            "role_part_file_import_artifact",
            "schema_part_iges_artifact_v1",
            "freecad_part_file_import",
            "iges",
        ),
        (
            ReviewedInputKind.STEP,
            _STEP,
            "model/step",
            "role_part_file_import_artifact",
            "schema_part_step_artifact_v1",
            "freecad_part_file_import",
            "step",
        ),
        (
            ReviewedInputKind.PNG,
            _PNG,
            "image/png",
            "role_imageplane_artifact",
            "schema_imageplane_png_artifact_v1",
            "freecad_imageplane",
            "place_or_edit_image_plane",
        ),
        (
            ReviewedInputKind.JPEG,
            _JPEG,
            "image/jpeg",
            "role_imageplane_artifact",
            "schema_imageplane_jpeg_artifact_v1",
            "freecad_imageplane",
            "place_or_edit_image_plane",
        ),
    ),
)
def test_closed_kind_descriptor_derives_exact_product_authority(
    tmp_path: Path,
    kind: ReviewedInputKind,
    payload: bytes,
    media_type: str,
    role: str,
    schema: str,
    family: str,
    operation: str,
) -> None:
    root = _private(tmp_path / "data")
    store = _store(root)
    receipt = store.seal(
        task_id=_TASK_ID,
        project_id=_PROJECT_ID,
        base_revision=_BASE_REVISION,
        inputs=(
            TrustedReviewedInputBytes(
                descriptor=_descriptor(kind, payload),
                payload=payload,
            ),
        ),
    )
    record = receipt.records[0]
    assert (
        record.media_type,
        record.role_term_ref_id,
        record.schema_term_ref_id,
        record.family_id,
        record.operation_ids,
    ) == (media_type, role, schema, family, (operation,))
    store.discard(
        task_id=_TASK_ID,
        project_id=_PROJECT_ID,
        base_revision=_BASE_REVISION,
    )
    store.close()


def test_store_seals_exact_bytes_and_fd_then_cleans_run_and_catalog(tmp_path: Path) -> None:
    root = _private(tmp_path / "data")
    source = tmp_path / "source.png"
    source.write_bytes(_PNG)
    source.chmod(0o600)
    source_fd = os.open(source, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    store = _store(root)
    try:
        receipt = store.seal(
            task_id=_TASK_ID,
            project_id=_PROJECT_ID,
            base_revision=_BASE_REVISION,
            inputs=(
                TrustedReviewedInputBytes(
                    descriptor=_descriptor(ReviewedInputKind.STEP, _STEP),
                    payload=_STEP,
                ),
                TrustedReviewedInputFileDescriptor(
                    descriptor=_descriptor(ReviewedInputKind.PNG, _PNG),
                    fd=source_fd,
                ),
            ),
        )
    finally:
        os.close(source_fd)

    assert tuple(
        sorted(
            (
                record.media_type,
                record.role_term_ref_id,
                record.schema_term_ref_id,
                record.family_id,
                record.operation_ids,
                record.maximum_bytes,
            )
            for record in receipt.records
        )
    ) == (
        (
            "image/png",
            "role_imageplane_artifact",
            "schema_imageplane_png_artifact_v1",
            "freecad_imageplane",
            ("place_or_edit_image_plane",),
            4 * 1024 * 1024,
        ),
        (
            "model/step",
            "role_part_file_import_artifact",
            "schema_part_step_artifact_v1",
            "freecad_part_file_import",
            ("step",),
            4 * 1024 * 1024,
        ),
    )
    catalog_root = root / REVIEWED_INPUT_CATALOG_DIRECTORY
    catalog = next(path for path in catalog_root.iterdir() if path.name.startswith("catalog_"))
    assert stat.S_IMODE(catalog.lstat().st_mode) == 0o700
    assert set(path.name for path in catalog.iterdir()) == {
        REVIEWED_INPUT_CATALOG_MANIFEST,
        *(record.artifact_id for record in receipt.records),
    }
    for path in catalog.iterdir():
        assert stat.S_IMODE(path.lstat().st_mode) == 0o600
    manifest = (catalog / REVIEWED_INPUT_CATALOG_MANIFEST).read_bytes()
    assert (
        manifest
        == json.dumps(
            receipt.to_mapping(),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode()
    )

    store.close()
    store = _store(root)
    assert store.requires_artifact_snapshot(_program()) is True
    lease = store.acquire(
        task_id=_TASK_ID,
        project_id=_PROJECT_ID,
        base_revision=_BASE_REVISION,
        run_id=_RUN_ID,
    )
    run_directories = tuple(
        path for path in catalog_root.iterdir() if path.name.startswith(".run_")
    )
    assert len(run_directories) == 1
    duplicate = lease.duplicate_directory_fd()
    try:
        assert set(os.listdir(duplicate)) == {
            REVIEWED_ARTIFACT_MANIFEST_NAME,
            *(record.artifact_id for record in receipt.records),
        }
    finally:
        os.close(duplicate)
    lease.close()
    assert not tuple(path for path in catalog_root.iterdir() if path.name.startswith(".run_"))

    store.discard(
        task_id=_TASK_ID,
        project_id=_PROJECT_ID,
        base_revision=_BASE_REVISION,
    )
    assert tuple(catalog_root.iterdir()) == ()
    assert store.requires_artifact_snapshot(_program()) is False
    store.close()


def test_seal_failure_removes_partial_private_stage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _private(tmp_path / "data")
    store = _store(root)
    original = ingress_module._write_file  # noqa: SLF001
    calls = 0

    def fail_second(directory_fd: int, name: str, payload: bytes) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("synthetic host write failure")
        original(directory_fd, name, payload)

    monkeypatch.setattr(ingress_module, "_write_file", fail_second)
    with pytest.raises(ReviewedInputIngressError) as caught:
        store.seal(
            task_id=_TASK_ID,
            project_id=_PROJECT_ID,
            base_revision=_BASE_REVISION,
            inputs=(
                TrustedReviewedInputBytes(
                    descriptor=_descriptor(ReviewedInputKind.STEP, _STEP),
                    payload=_STEP,
                ),
            ),
        )
    assert caught.value.code is ReviewedInputIngressErrorCode.STORE_FAILURE
    catalog_root = root / REVIEWED_INPUT_CATALOG_DIRECTORY
    assert tuple(catalog_root.iterdir()) == ()
    store.close()


def test_ingress_rejects_digest_budget_and_nonprivate_fd(tmp_path: Path) -> None:
    root = _private(tmp_path / "data")
    store = _store(root)
    with pytest.raises(ReviewedInputIngressError) as digest_failure:
        store.seal(
            task_id=_TASK_ID,
            project_id=_PROJECT_ID,
            base_revision=_BASE_REVISION,
            inputs=(
                TrustedReviewedInputBytes(
                    descriptor=TrustedReviewedInputDescriptor(
                        kind=ReviewedInputKind.STEP,
                        content_sha256="0" * 64,
                        size_bytes=len(_STEP),
                    ),
                    payload=_STEP,
                ),
            ),
        )
    assert digest_failure.value.code is ReviewedInputIngressErrorCode.INTEGRITY_FAILURE

    with pytest.raises(ReviewedInputIngressError) as budget_failure:
        TrustedReviewedInputDescriptor(
            kind=ReviewedInputKind.STEP,
            content_sha256="0" * 64,
            size_bytes=4 * 1024 * 1024 + 1,
        )
    assert budget_failure.value.code is ReviewedInputIngressErrorCode.BUDGET_EXCEEDED

    source = tmp_path / "public.step"
    source.write_bytes(_STEP)
    source.chmod(0o644)
    source_fd = os.open(source, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    try:
        with pytest.raises(ReviewedInputIngressError) as fd_failure:
            store.seal(
                task_id=_TASK_ID,
                project_id=_PROJECT_ID,
                base_revision=_BASE_REVISION,
                inputs=(
                    TrustedReviewedInputFileDescriptor(
                        descriptor=_descriptor(ReviewedInputKind.STEP, _STEP),
                        fd=source_fd,
                    ),
                ),
            )
    finally:
        os.close(source_fd)
    assert fd_failure.value.code is ReviewedInputIngressErrorCode.INVALID_INPUT
    assert not (root / REVIEWED_INPUT_CATALOG_DIRECTORY).exists()
    store.close()


def test_reloaded_catalog_rejects_recomputed_wrong_role(tmp_path: Path) -> None:
    root = _private(tmp_path / "data")
    store = _store(root)
    store.seal(
        task_id=_TASK_ID,
        project_id=_PROJECT_ID,
        base_revision=_BASE_REVISION,
        inputs=(
            TrustedReviewedInputBytes(
                descriptor=_descriptor(ReviewedInputKind.STEP, _STEP),
                payload=_STEP,
            ),
        ),
    )
    catalog_root = root / REVIEWED_INPUT_CATALOG_DIRECTORY
    catalog = next(path for path in catalog_root.iterdir() if path.name.startswith("catalog_"))
    manifest = catalog / REVIEWED_INPUT_CATALOG_MANIFEST
    mapping = json.loads(manifest.read_text())
    mapping["records"][0]["role_term_ref_id"] = "role_wrong"
    body = {key: value for key, value in mapping.items() if key != "catalog_sha256"}
    raw_body = json.dumps(
        body,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode()
    mapping["catalog_sha256"] = hashlib.sha256(
        ingress_module._CATALOG_DIGEST_DOMAIN + raw_body  # noqa: SLF001
    ).hexdigest()
    manifest.write_bytes(
        json.dumps(
            mapping,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode()
    )

    with pytest.raises(ReviewedInputIngressError) as caught:
        store.requires_artifact_snapshot(_program())
    assert caught.value.code is ReviewedInputIngressErrorCode.INTEGRITY_FAILURE
    store.close()


def test_application_host_attach_bind_preflight_acquire_close_discard(tmp_path: Path) -> None:
    data_root = _private(tmp_path / "home") / "data"
    application = AgentApplication.open(data_root=data_root)
    project = application.bootstrap_empty().head
    task_id = "task_11111111111111111111111111111111"
    created = application.create_task(
        task_id=task_id,
        project_id=project.project_id,
        reasoning_owner=ReasoningOwner.EXTERNAL_PLAN,
        review_policy=ReviewPolicy.REQUIRE_REVIEW,
    )
    task = created.task_run
    receipt = application.seal_reviewed_task_inputs(
        task_id=task.id,
        project_id=task.project_id,
        base_revision=task.base_revision,
        inputs=(
            TrustedReviewedInputBytes(
                descriptor=_descriptor(ReviewedInputKind.STEP, _STEP),
                payload=_STEP,
            ),
        ),
    )
    validated = _program(task.id, task.base_revision)

    with application._cad_gate:  # noqa: SLF001
        port = application._cad_execution_port_under_gate()  # noqa: SLF001
    assert port._task_input_snapshot_provider is application._reviewed_inputs  # noqa: SLF001
    assert port._task_input_preflight is application._reviewed_inputs  # noqa: SLF001
    assert port._task_input_preflight.requires_artifact_snapshot(validated) is True  # noqa: SLF001

    lease = port._task_input_snapshot_provider.acquire(  # noqa: SLF001
        task_id=task.id,
        project_id=task.project_id,
        base_revision=task.base_revision,
        run_id="run_22222222222222222222222222222222",
    )
    assert lease.snapshot.records == receipt.records
    assert lease.snapshot.task_id == task.id
    assert lease.snapshot.project_id == task.project_id
    assert lease.snapshot.base_revision == task.base_revision
    lease.close()

    application.discard_reviewed_task_inputs(
        task_id=task.id,
        project_id=task.project_id,
        base_revision=task.base_revision,
    )
    assert port._task_input_preflight.requires_artifact_snapshot(validated) is False  # noqa: SLF001
    application.close()


def test_application_rejects_attachment_binding_for_another_task(tmp_path: Path) -> None:
    data_root = _private(tmp_path / "home") / "data"
    application = AgentApplication.open(data_root=data_root)
    project = application.bootstrap_empty().head
    task_id = "task_33333333333333333333333333333333"
    created = application.create_task(
        task_id=task_id,
        project_id=project.project_id,
        reasoning_owner=ReasoningOwner.EXTERNAL_PLAN,
        review_policy=ReviewPolicy.REQUIRE_REVIEW,
    )
    task = created.task_run

    with pytest.raises(ReviewedInputIngressError) as caught:
        application.seal_reviewed_task_inputs(
            task_id=task.id,
            project_id=_PROJECT_ID,
            base_revision=task.base_revision,
            inputs=(
                TrustedReviewedInputBytes(
                    descriptor=_descriptor(ReviewedInputKind.STEP, _STEP),
                    payload=_STEP,
                ),
            ),
        )
    assert caught.value.code is ReviewedInputIngressErrorCode.AUTHORITY_VIOLATION
    application.close()
