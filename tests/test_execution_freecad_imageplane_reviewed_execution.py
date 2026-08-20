"""Focused family-only product tests for reviewed ``Image::ImagePlane``."""

from __future__ import annotations

import dataclasses
import hashlib
import math
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

import vibecad.execution.freecad_imageplane_reviewed_execution as product
from tests.test_intent_bridge_freecad_imageplane_adapter import (
    _graph,
    _lower,
    _request,
    _Sink,
)
from vibecad import _file_compat
from vibecad.engine.document_assets import DocumentAssetWorkspace
from vibecad.execution.freecad_reviewed_intent_execution import (
    ReviewedIntentExecutionError,
    _ReviewedFamilyExecutionContext,
    _ReviewedFamilyNativeExecution,
)
from vibecad.execution.selectors import Provenance, ProvenanceSource, SemanticRole
from vibecad.intent_bridge.freecad_imageplane_adapter import (
    IMAGEPLANE_MANIFEST,
    IMAGEPLANE_OPERATION_SPEC,
    build_imageplane_artifact_document,
)
from vibecad.intent_bridge.ports import read_verified_document
from vibecad.parametric import freecad_imageplane_rules as imageplane_rules
from vibecad.parametric.freecad_imageplane_rules import (
    MAX_IMAGEPLANE_ARTIFACT_BYTES,
    HostOwnedImageStager,
    ImagePlaneBackendPlan,
    ImagePlaneConformanceReceipt,
    ImagePlaneExecutionBindings,
)
from vibecad.validation.contracts import EntityObservation

_IMAGE = (
    Path(__file__).parent / "fixtures" / "guided_photo_v1" / "images" / "calibration_block.png"
).read_bytes()


class _ExactReader:
    def __init__(self, document: object, payload: bytes) -> None:
        self.document = document
        self.payload = payload
        self.calls = 0

    def read(self, document: object, maximum_bytes: int) -> bytes:
        if document != self.document or len(self.payload) > maximum_bytes:
            raise RuntimeError("wrong exact artifact")
        self.calls += 1
        return self.payload


class _Vector:
    def __init__(self, x: float, y: float, z: float) -> None:
        self.x = x
        self.y = y
        self.z = z


class _Rotation:
    def __init__(self, q: tuple[float, float, float, float]) -> None:
        self.Q = q


class _Feature:
    def __init__(
        self,
        *,
        document: _Document,
        plan: ImagePlaneBackendPlan,
        image_file: Path,
    ) -> None:
        self.Document = document
        self.TypeId = "Image::ImagePlane"
        self.Name = imageplane_rules._object_name(plan)  # noqa: SLF001
        self.Label = "model label is not authority"
        self.PropertiesList = (
            "VibeCADImagePlaneKey",
            "VibeCADImagePlaneGraphId",
            "VibeCADImagePlaneNodeId",
        )
        self.VibeCADImagePlaneKey = imageplane_rules._binding_sha256(plan)  # noqa: SLF001
        self.VibeCADImagePlaneGraphId = plan.source_graph_id
        self.VibeCADImagePlaneNodeId = plan.node_id
        self.ExpressionEngine = ()
        self.State = ("Up-to-date",)
        self.ImageFile = str(image_file)
        self.XSize = float(plan.configuration["x_size_mm"])
        self.YSize = float(plan.configuration["y_size_mm"])
        placement = plan.configuration["placement"]
        axis = placement["axis"]
        angle = float(placement["angle_degrees"])
        half = angle * 3.141592653589793 / 360.0
        sine = math.sin(half)
        quaternion = (
            float(axis[0]) * sine,
            float(axis[1]) * sine,
            float(axis[2]) * sine,
            math.cos(half),
        )
        self.Placement = SimpleNamespace(
            Base=_Vector(*(float(item) for item in placement["position_mm"])),
            Rotation=_Rotation(quaternion),
        )

    def getParentGroup(self) -> None:
        return None

    def getTypeIdOfProperty(self, name: str) -> str:
        assert name in self.PropertiesList
        return "App::PropertyString"

    def getEditorMode(self, name: str) -> tuple[str, str]:
        assert name in self.PropertiesList
        return ("ReadOnly", "Hidden")

    def getPropertyStatus(self, name: str) -> tuple[str]:
        assert name in self.PropertiesList
        return ("LockDynamic",)

    def isValid(self) -> bool:
        return True


class _Document:
    def __init__(self) -> None:
        self.Name = "ImagePlaneProduct"
        self.TransientDir = ""
        self.FileName = ""
        self.UndoMode = 1
        self.HasPendingTransaction = False
        self.Objects: list[object] = []

    def getObject(self, name: str) -> object | None:
        return next((item for item in self.Objects if getattr(item, "Name", None) == name), None)

    def recompute(self) -> None:
        return None


def _lowered() -> tuple[object, object, ImagePlaneBackendPlan, bytes, object]:
    artifact = build_imageplane_artifact_document(_IMAGE, media_type="image/png")
    request, reader, policy = _request(_graph(artifact.content_sha256))
    adapter = product.imageplane_reviewed_adapter_factory(_Sink())
    result, receipt = _lower(adapter, request, reader, policy)
    plan, payload = adapter.read_plan(receipt)
    assert type(plan) is ImagePlaneBackendPlan
    return result, receipt, plan, payload, artifact


def _roots(tmp_path: Path) -> tuple[Path, Path]:
    assets = tmp_path / "assets"
    staging = tmp_path / "staging"
    for path in (assets, staging):
        path.mkdir(mode=0o700)
        os.chmod(path, 0o700)
        if os.name == "nt":
            _file_compat.set_private_dacl(path)
    return assets, staging


def _artifact_context(
    tmp_path: Path,
    document: _Document,
    artifact: object,
) -> tuple[product.ImagePlaneReviewedArtifactContext, _ExactReader]:
    assets, staging = _roots(tmp_path)
    workspace = DocumentAssetWorkspace(assets)
    workspace.attach(document)
    reader = _ExactReader(artifact, _IMAGE)
    return (
        product.ImagePlaneReviewedArtifactContext(
            document_assets=workspace,
            artifact_document=artifact,
            artifacts=reader,
            stager=HostOwnedImageStager(staging),
        ),
        reader,
    )


def _fake_native_apply(
    plan: ImagePlaneBackendPlan,
    artifact_payload: bytes = _IMAGE,
):
    def apply(
        raw: bytes,
        *,
        expected_content_sha256: str,
        expected_plan_sha256: str,
        bindings: ImagePlaneExecutionBindings,
    ) -> ImagePlaneConformanceReceipt:
        assert raw == plan.canonical_bytes
        assert expected_content_sha256 == hashlib.sha256(raw).hexdigest()
        assert expected_plan_sha256 == plan.plan_sha256
        assert bindings.document_assets.require_attached(bindings.document) == Path(
            bindings.document.TransientDir
        )
        payload = read_verified_document(
            bindings.artifacts,
            bindings.artifact_document,
            maximum_bytes=MAX_IMAGEPLANE_ARTIFACT_BYTES,
        )
        assert payload == artifact_payload
        alias = plan.artifact_content_sha256 + ".png"
        retained = Path(bindings.document.TransientDir) / alias
        retained.write_bytes(payload)
        os.chmod(retained, 0o600)
        if os.name == "nt":
            _file_compat.set_private_dacl(retained)
        feature = bindings.document.getObject(imageplane_rules._object_name(plan))  # noqa: SLF001
        disposition = "updated"
        configured = _Feature(document=bindings.document, plan=plan, image_file=retained)
        if feature is None:
            feature = configured
            objects = bindings.document.Objects
            if isinstance(objects, list):
                objects.append(feature)
            else:
                bindings.document.Objects = (*objects, feature)
            disposition = "created"
        else:
            feature.ImageFile = configured.ImageFile
            feature.XSize = configured.XSize
            feature.YSize = configured.YSize
            feature.Placement = configured.Placement
        signature = imageplane_rules._placement_signature(feature.Placement)  # noqa: SLF001
        return ImagePlaneConformanceReceipt(
            plan_sha256=plan.plan_sha256,
            disposition=disposition,
            object_name=feature.Name,
            binding_sha256=feature.VibeCADImagePlaneKey,
            artifact_id=plan.artifact_id,
            artifact_content_sha256=plan.artifact_content_sha256,
            artifact_media_type=plan.artifact_media_type,
            retained_alias=alias,
            x_size_mm=feature.XSize,
            y_size_mm=feature.YSize,
            position_mm=signature[:3],
            rotation_quaternion=signature[3:],
        )

    return apply


def test_imageplane_family_spec_freezes_exact_reference_create_contract() -> None:
    spec = product.IMAGEPLANE_REVIEWED_FAMILY_SPEC
    contract = spec.result_contract
    identity = product.IMAGEPLANE_REVIEWED_PRODUCT_IDENTITIES[0]

    assert spec.manifest is IMAGEPLANE_MANIFEST
    assert spec.operation_ids == ("place_or_edit_image_plane",)
    assert contract.result_kind == "reference"
    assert contract.owned_type_ids == ("Image::ImagePlane",)
    assert contract.semantic_roles == (SemanticRole.SUPPORT,)
    assert contract.execution_modes == ("create", "update_primary")
    assert (contract.minimum_sources, contract.maximum_sources) == (0, 1)
    assert product.resolve_imageplane_reviewed_operation(*identity) is IMAGEPLANE_OPERATION_SPEC
    assert product.resolve_imageplane_reviewed_operation(identity[0], identity[1] + "x") is None


def test_imageplane_exact_adapter_and_plan_validator_reuse_existing_contract() -> None:
    result, receipt, plan, payload, artifact = _lowered()

    product.validate_imageplane_reviewed_plan(plan, receipt, IMAGEPLANE_OPERATION_SPEC)
    assert result.plan_document == receipt.plan_document
    assert plan.artifact_id == artifact.artifact_id
    assert plan.artifact_content_sha256 == artifact.content_sha256
    assert payload == plan.canonical_bytes
    assert b'"path"' not in payload
    assert b"ImageFile" not in payload

    rebound = dataclasses.replace(receipt, request_digest="f" * 64)
    with pytest.raises(ReviewedIntentExecutionError):
        product.validate_imageplane_reviewed_plan(plan, rebound, IMAGEPLANE_OPERATION_SPEC)


def test_shared_callback_missing_artifact_context_is_inert() -> None:
    _, receipt, plan, payload, _ = _lowered()
    document = _Document()
    session = SimpleNamespace(doc=document)

    with pytest.raises(ReviewedIntentExecutionError):
        product.execute_imageplane_reviewed_plan(
            document,
            plan,
            payload,
            receipt.plan_document,
            IMAGEPLANE_OPERATION_SPEC,
            _ReviewedFamilyExecutionContext(
                session=session,
                document=document,
                source_results=(),
            ),
        )

    assert document.Objects == []
    assert document.TransientDir == ""


def test_authenticated_artifact_hook_creates_exact_reference_and_detects_asset_tamper(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _, receipt, plan, payload, artifact = _lowered()
    document = _Document()
    artifact_context, reader = _artifact_context(tmp_path, document, artifact)
    session = SimpleNamespace(
        doc=document,
        _document_assets=artifact_context.document_assets,
    )
    monkeypatch.setattr(product, "apply_imageplane_plan", _fake_native_apply(plan))

    execution = product.execute_imageplane_reviewed_plan_with_artifacts(
        document,
        plan,
        payload,
        receipt.plan_document,
        IMAGEPLANE_OPERATION_SPEC,
        artifact_context,
        session=session,
    )

    assert type(execution) is _ReviewedFamilyNativeExecution
    assert execution.object is document.Objects[0]
    assert execution.object.TypeId == "Image::ImagePlane"
    assert execution.object.getParentGroup() is None
    assert reader.calls == 1
    assert Path(execution.object.ImageFile).parent == Path(document.TransientDir)
    assert Path(execution.object.ImageFile).name == execution.receipt.native_receipt.retained_alias
    assert (
        execution.receipt.native_receipt.artifact_content_sha256
        == hashlib.sha256(_IMAGE).hexdigest()
    )
    observation = EntityObservation(
        object_id="object_0123456789abcdef0123456789abcdef",
        feature_id="feature_0123456789abcdef0123456789abcdef",
        object_type="Image::ImagePlane",
        semantic_role=SemanticRole.SUPPORT.value,
        provenance=Provenance(
            source=ProvenanceSource.MODEL,
            operation_id="apply_reviewed_intent",
        ).to_mapping(),
        placement=imageplane_rules._placement_signature(execution.object.Placement),  # noqa: SLF001
    )
    execution.receipt.validate_adoption(document, execution.object, observation)

    Path(execution.object.ImageFile).write_bytes(b"tampered retained bytes")
    os.chmod(execution.object.ImageFile, 0o600)
    with pytest.raises(ReviewedIntentExecutionError):
        execution.receipt.validate_adoption(document, execution.object, observation)


def test_existing_binding_is_rejected_before_artifact_read_or_native_mutation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _, receipt, plan, payload, artifact = _lowered()
    document = _Document()
    artifact_context, reader = _artifact_context(tmp_path, document, artifact)
    workspace = Path(document.TransientDir)
    retained = workspace / (plan.artifact_content_sha256 + ".png")
    retained.write_bytes(_IMAGE)
    os.chmod(retained, 0o600)
    existing = _Feature(document=document, plan=plan, image_file=retained)
    document.Objects.append(existing)
    session = SimpleNamespace(
        doc=document,
        _document_assets=artifact_context.document_assets,
    )
    called = False

    def unreachable(*args: object, **kwargs: object) -> object:
        nonlocal called
        called = True
        raise AssertionError("native rule must not receive a CREATE/edit ambiguity")

    monkeypatch.setattr(product, "apply_imageplane_plan", unreachable)

    with pytest.raises(ReviewedIntentExecutionError):
        product.execute_imageplane_reviewed_plan_with_artifacts(
            document,
            plan,
            payload,
            receipt.plan_document,
            IMAGEPLANE_OPERATION_SPEC,
            artifact_context,
            session=session,
        )

    assert not called
    assert reader.calls == 0
    assert document.Objects == [existing]
    assert retained.read_bytes() == _IMAGE
