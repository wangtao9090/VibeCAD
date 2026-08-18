"""Focused unregistered integration for reviewed artifact families."""

from __future__ import annotations

import hashlib
import os
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

import vibecad.execution.freecad_imageplane_reviewed_execution as image_product
import vibecad.execution.freecad_reviewed_intent_execution as shared
from tests import test_execution_freecad_imageplane_reviewed_execution as image_fakes
from tests import test_execution_freecad_part_file_import_reviewed_execution as import_fakes
from tests.test_intent_bridge_freecad_imageplane_adapter import (
    _graph as image_graph,
)
from tests.test_intent_bridge_freecad_imageplane_adapter import (
    _lower as image_lower,
)
from tests.test_intent_bridge_freecad_imageplane_adapter import (
    _request as image_request,
)
from tests.test_intent_bridge_freecad_imageplane_adapter import (
    _Sink as ImageSink,
)
from tests.test_intent_bridge_freecad_part_file_import_adapter import (
    _graph as import_graph,
)
from tests.test_intent_bridge_freecad_part_file_import_adapter import (
    _lower as import_lower,
)
from tests.test_intent_bridge_freecad_part_file_import_adapter import (
    _request as import_request,
)
from tests.test_intent_bridge_freecad_part_file_import_adapter import (
    _Sink as ImportSink,
)
from tests.test_program_executor import _FakeShape
from tests.test_reviewed_intent_program import reviewed_box_program
from vibecad.engine.document_assets import DocumentAssetWorkspace
from vibecad.execution.freecad_imageplane_reviewed_execution import (
    build_imageplane_reviewed_family_descriptor,
)
from vibecad.execution.freecad_part_file_import_reviewed_execution import (
    build_part_file_import_reviewed_family_descriptor,
)
from vibecad.execution.freecad_reviewed_artifact_inputs import (
    MAX_REVIEWED_ARTIFACT_BYTES,
    ReviewedArtifactCatalogRecord,
    ReviewedArtifactCatalogSnapshot,
    _ReviewedArtifactRunResolver,
)
from vibecad.intent_bridge.freecad_imageplane_adapter import (
    IMAGEPLANE_OPERATION_SPEC,
    FreeCADImagePlaneAdapter,
    build_imageplane_artifact_document,
)
from vibecad.intent_bridge.freecad_part_file_import_adapter import (
    FreeCADPartFileImportAdapter,
    build_part_file_import_artifact_document,
)
from vibecad.parametric.freecad_imageplane_rules import HostOwnedImageStager
from vibecad.parametric.freecad_part_file_import_rules import (
    PART_FILE_IMPORT_NATIVE_SPECS,
    HostOwnedImportStager,
    PartFileImportOperation,
)

_IMAGE = image_fakes._IMAGE  # noqa: SLF001


class _PayloadSource:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload
        self.reads = 0
        self.closed = False

    def read(self, record: ReviewedArtifactCatalogRecord, maximum_bytes: int) -> bytes:
        if self.closed or len(self.payload) > maximum_bytes:
            raise RuntimeError
        self.reads += 1
        return self.payload

    def close(self) -> None:
        self.closed = True


class _StagerFactory:
    def __init__(self, stager: object) -> None:
        self.stager = stager
        self.creates: list[tuple[str, str]] = []
        self.closed = False

    def create(
        self,
        *,
        record: ReviewedArtifactCatalogRecord,
        family_id: str,
        operation_id: str,
    ) -> object:
        if self.closed or record.family_id != family_id or operation_id not in record.operation_ids:
            raise RuntimeError
        self.creates.append((family_id, operation_id))
        return self.stager

    def close(self) -> None:
        self.closed = True


def _private_root(path: Path) -> Path:
    path.mkdir(mode=0o700, parents=True)
    os.chmod(path, 0o700)
    return path


def _resolver(
    *,
    artifact: object,
    family_id: str,
    operation_id: str,
    payload: bytes,
    stager: object,
    token: object,
    mode: str = "exact",
) -> tuple[_ReviewedArtifactRunResolver, _PayloadSource, _StagerFactory]:
    records: tuple[ReviewedArtifactCatalogRecord, ...]
    if mode == "missing":
        records = ()
    else:
        content_sha256 = artifact.content_sha256 if mode == "exact" else "0" * 64
        records = (
            ReviewedArtifactCatalogRecord(
                artifact_id=artifact.artifact_id,
                content_sha256=content_sha256,
                size_bytes=artifact.size_bytes,
                media_type=artifact.media_type,
                role_term_ref_id=artifact.role_term_ref_id,
                schema_term_ref_id=artifact.schema_term_ref_id,
                document_id=artifact.document_id,
                family_id=family_id,
                operation_ids=(operation_id,),
                maximum_bytes=MAX_REVIEWED_ARTIFACT_BYTES,
            ),
        )
    snapshot = ReviewedArtifactCatalogSnapshot(
        task_id="task_artifact_integration",
        project_id="project_artifact_integration",
        base_revision="revision_artifact_integration",
        run_id="run_artifact_integration",
        records=records,
    )
    source = _PayloadSource(payload)
    stagers = _StagerFactory(stager)
    return (
        _ReviewedArtifactRunResolver(
            snapshot=snapshot,
            source=source,
            stager_factory=stagers,
            task_id=snapshot.task_id,
            project_id=snapshot.project_id,
            base_revision=snapshot.base_revision,
            run_id=snapshot.run_id,
            run_token=token,
        ),
        source,
        stagers,
    )


def _unregistered_route(family: object, operation: object) -> shared.ReviewedIntentRoute:
    route = object.__new__(shared.ReviewedIntentRoute)
    object.__setattr__(
        route,
        "operation_id",
        f"{family.manifest.family_id}.{operation.operation_id}",
    )
    object.__setattr__(route, "semantic_operation", "unregistered.test.identity")
    object.__setattr__(route, "family", family)
    object.__setattr__(route, "manifest", family.manifest)
    object.__setattr__(route, "operation", operation)
    object.__setattr__(
        route,
        "subject_type_term",
        family.intent_binding.subject_type_for(operation),
    )
    object.__setattr__(route, "manifest_semantic_operation", "unregistered.test.identity")
    object.__setattr__(route, "route_contract_sha256", "a" * 64)
    return route


def _install_unregistered(
    monkeypatch: pytest.MonkeyPatch,
    route: shared.ReviewedIntentRoute,
    *,
    result: object,
    receipt: object,
    plan: object,
    payload: bytes,
) -> None:
    lowered = shared.LoweredReviewedIntent(
        route=route,
        result=result,
        receipt=receipt,
        plan=plan,
        payload=payload,
    )
    monkeypatch.setitem(sys.modules, "FreeCAD", ModuleType("FreeCAD"))
    monkeypatch.setattr(shared, "route_reviewed_intent", lambda value: route)
    monkeypatch.setattr(shared, "lower_reviewed_intent", lambda value: lowered)
    monkeypatch.setattr(shared, "require_reviewed_route_verified", lambda route, *, freecad: None)


@pytest.mark.parametrize("operation", tuple(PartFileImportOperation))
def test_unregistered_import_routes_resolve_and_narrow_exact_artifacts(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    operation: PartFileImportOperation,
) -> None:
    artifact_payload = f"resolver-{operation.value}".encode()
    artifact = build_part_file_import_artifact_document(operation, artifact_payload)
    request, reader, policy = import_request(import_graph(operation, artifact.content_sha256))
    adapter = FreeCADPartFileImportAdapter(ImportSink())
    result, receipt = import_lower(adapter, request, reader, policy)
    plan, payload = adapter.read_plan(receipt)
    family = build_part_file_import_reviewed_family_descriptor()
    route = _unregistered_route(family, receipt.operation)
    _install_unregistered(
        monkeypatch,
        route,
        result=result,
        receipt=receipt,
        plan=plan,
        payload=payload,
    )
    document = import_fakes._Document()  # noqa: SLF001
    calls = import_fakes._install_fake_apply(  # noqa: SLF001
        monkeypatch,
        document,
        len(artifact_payload),
    )
    staging = _private_root(tmp_path / "staging")
    token = object()
    resolver, source, stagers = _resolver(
        artifact=artifact,
        family_id=family.manifest.family_id,
        operation_id=operation.value,
        payload=artifact_payload,
        stager=HostOwnedImportStager(staging),
        token=token,
    )

    executed = shared.execute_reviewed_intent_native(
        SimpleNamespace(doc=document),
        reviewed_box_program(),
        _reviewed_run_token=token,
        _reviewed_artifact_resolver=resolver,
    )

    assert executed.object is document.Objects[0]
    assert executed.object.TypeId == PART_FILE_IMPORT_NATIVE_SPECS[operation].type_id
    assert source.reads == 1
    assert stagers.creates == [(family.manifest.family_id, operation.value)]
    assert len(calls) == 1 and calls[0].artifact_document == artifact
    assert tuple(staging.iterdir()) == ()
    resolver.close()


@pytest.mark.parametrize("mode", ("missing", "tamper"))
def test_unregistered_import_resolver_failure_is_pre_mutation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    mode: str,
) -> None:
    operation = PartFileImportOperation.STEP
    artifact_payload = b"resolver-step"
    artifact = build_part_file_import_artifact_document(operation, artifact_payload)
    request, reader, policy = import_request(import_graph(operation, artifact.content_sha256))
    adapter = FreeCADPartFileImportAdapter(ImportSink())
    result, receipt = import_lower(adapter, request, reader, policy)
    plan, payload = adapter.read_plan(receipt)
    family = build_part_file_import_reviewed_family_descriptor()
    route = _unregistered_route(family, receipt.operation)
    _install_unregistered(
        monkeypatch,
        route,
        result=result,
        receipt=receipt,
        plan=plan,
        payload=payload,
    )
    document = import_fakes._Document()  # noqa: SLF001
    native_calls = import_fakes._install_fake_apply(  # noqa: SLF001
        monkeypatch,
        document,
        len(artifact_payload),
    )
    token = object()
    resolver, source, stagers = _resolver(
        artifact=artifact,
        family_id=family.manifest.family_id,
        operation_id=operation.value,
        payload=artifact_payload,
        stager=HostOwnedImportStager(_private_root(tmp_path / "staging")),
        token=token,
        mode=mode,
    )

    with pytest.raises(shared.ReviewedIntentExecutionError):
        shared.execute_reviewed_intent_native(
            SimpleNamespace(doc=document),
            reviewed_box_program(),
            _reviewed_run_token=token,
            _reviewed_artifact_resolver=resolver,
        )

    assert document.Objects == [] and native_calls == []
    assert source.reads == 0 and stagers.creates == []
    resolver.close()


def _image_case(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    mode: str = "exact",
) -> tuple[object, object, _ReviewedArtifactRunResolver, _PayloadSource, _StagerFactory]:
    artifact = build_imageplane_artifact_document(_IMAGE, media_type="image/png")
    request, reader, policy = image_request(image_graph(artifact.content_sha256))
    adapter = FreeCADImagePlaneAdapter(ImageSink())
    result, receipt = image_lower(adapter, request, reader, policy)
    plan, payload = adapter.read_plan(receipt)
    family = build_imageplane_reviewed_family_descriptor()
    route = _unregistered_route(family, IMAGEPLANE_OPERATION_SPEC)
    _install_unregistered(
        monkeypatch,
        route,
        result=result,
        receipt=receipt,
        plan=plan,
        payload=payload,
    )
    document = image_fakes._Document()  # noqa: SLF001
    assets = DocumentAssetWorkspace(_private_root(tmp_path / "assets"))
    assets.attach(document)
    session = SimpleNamespace(doc=document, _document_assets=assets)
    monkeypatch.setattr(
        image_product,
        "apply_imageplane_plan",
        image_fakes._fake_native_apply(plan),  # noqa: SLF001
    )
    token = object()
    resolver, source, stagers = _resolver(
        artifact=artifact,
        family_id=family.manifest.family_id,
        operation_id=IMAGEPLANE_OPERATION_SPEC.operation_id,
        payload=_IMAGE,
        stager=HostOwnedImageStager(_private_root(tmp_path / "staging")),
        token=token,
        mode=mode,
    )
    return session, token, resolver, source, stagers


def test_unregistered_image_resolves_commits_and_late_rolls_back_workspace(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    session, token, resolver, source, stagers = _image_case(monkeypatch, tmp_path / "commit")
    committed = shared.execute_reviewed_intent_native(
        session,
        reviewed_box_program(),
        _reviewed_run_token=token,
        _reviewed_artifact_resolver=resolver,
    )
    retained = Path(committed.object.ImageFile)
    assert retained.read_bytes() == _IMAGE
    assert shared._commit_reviewed_native_create(committed) is True  # noqa: SLF001
    assert retained.exists() and source.reads == 1 and len(stagers.creates) == 1
    resolver.close()

    session, token, resolver, source, stagers = _image_case(monkeypatch, tmp_path / "rollback")
    rolled_back = shared.execute_reviewed_intent_native(
        session,
        reviewed_box_program(),
        _reviewed_run_token=token,
        _reviewed_artifact_resolver=resolver,
    )
    retained = Path(rolled_back.object.ImageFile)
    session.doc.Objects = []
    assert shared._rollback_reviewed_native_create(rolled_back) is True  # noqa: SLF001
    assert not retained.exists()
    assert tuple(Path(session.doc.TransientDir).iterdir()) == ()
    assert source.reads == 1 and len(stagers.creates) == 1
    resolver.close()


@pytest.mark.parametrize("mode", ("missing", "tamper"))
def test_unregistered_image_resolver_failure_is_pre_mutation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    mode: str,
) -> None:
    session, token, resolver, source, stagers = _image_case(
        monkeypatch,
        tmp_path,
        mode=mode,
    )

    with pytest.raises(shared.ReviewedIntentExecutionError):
        shared.execute_reviewed_intent_native(
            session,
            reviewed_box_program(),
            _reviewed_run_token=token,
            _reviewed_artifact_resolver=resolver,
        )

    assert session.doc.Objects == []
    assert tuple(Path(session.doc.TransientDir).iterdir()) == ()
    assert source.reads == 0 and stagers.creates == []
    resolver.close()


def test_artifact_families_remain_unregistered_and_nonartifact_context_is_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert len(shared.CURRENT_REVIEWED_INTENT_ROUTES) == 78
    assert not any(
        route.manifest.family_id in {"freecad_part_file_import", "freecad_imageplane"}
        for route in shared.CURRENT_REVIEWED_INTENT_ROUTES
    )
    base = shared.REVIEWED_PART_BOX_ROUTE.family
    seen: list[object] = []

    def execute(document, plan, payload, plan_document, operation, context):
        del plan, payload, operation
        seen.append(context.artifact_context)
        obj = context.session.identity_object
        obj.TypeId = shared.REVIEWED_PART_BOX_ROUTE.operation.native_type_id
        obj.Shape = _FakeShape(volume=480.0)
        document.Objects = (*document.Objects, obj)
        return shared._ReviewedFamilyNativeExecution(  # noqa: SLF001
            object=obj,
            receipt=SimpleNamespace(
                plan_sha256=plan_document.document_digest,
                receipt_sha256=hashlib.sha256(b"nonartifact").hexdigest(),
            ),
        )

    family = shared._ReviewedIntentFamilyDescriptor(  # noqa: SLF001
        manifest=base.manifest,
        subject_type_term=base.subject_type_term,
        adapter_factory=base.adapter_factory,
        validate_plan=base.validate_plan,
        execute_plan=execute,
        product_results=base.product_results,
    )
    route = _unregistered_route(family, shared.REVIEWED_PART_BOX_ROUTE.operation)
    original = shared.lower_reviewed_intent(reviewed_box_program())
    _install_unregistered(
        monkeypatch,
        route,
        result=original.result,
        receipt=original.receipt,
        plan=original.plan,
        payload=original.payload,
    )
    session = SimpleNamespace(
        doc=SimpleNamespace(Objects=()),
        identity_object=SimpleNamespace(TypeId="Part::Box"),
    )

    shared.execute_reviewed_intent_native(session, reviewed_box_program())

    assert seen == [None]
