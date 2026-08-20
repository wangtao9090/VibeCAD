"""Program-run integration for registered Import and ImagePlane artifacts."""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

import vibecad.execution.executor as executor_module
import vibecad.execution.freecad_imageplane_reviewed_execution as image_execution
import vibecad.execution.freecad_part_file_import_reviewed_execution as import_execution
import vibecad.execution.freecad_planar_mechanical_reviewed_execution as pm_execution
import vibecad.execution.freecad_reviewed_intent_execution as reviewed_execution
from tests import test_execution_freecad_imageplane_reviewed_execution as image_fakes
from tests import test_execution_freecad_planar_mechanical_reviewed_execution as pm_fakes
from tests.test_intent_bridge_freecad_imageplane_adapter import (
    _configuration as image_configuration,
)
from tests.test_intent_bridge_freecad_imageplane_adapter import _graph as image_graph
from tests.test_intent_bridge_freecad_part_file_import_adapter import (
    _graph as import_graph,
)
from tests.test_program_executor import (
    BASE_REVISION,
    _active,
    _command,
    _FakePlacement,
    _FakeSession,
    _FakeShape,
    _store,
)
from tests.test_reviewed_artifact_family_integration import (
    _resolver,
    _unregistered_route,
)
from tests.test_reviewed_intent_program import reviewed_box_program
from vibecad import _file_compat
from vibecad.engine.document_assets import DocumentAssetWorkspace
from vibecad.execution.executor import InProcessCadExecutor
from vibecad.execution.freecad_imageplane_reviewed_execution import (
    build_imageplane_reviewed_family_descriptor,
)
from vibecad.execution.freecad_part_file_import_reviewed_execution import (
    build_part_file_import_reviewed_family_descriptor,
)
from vibecad.execution.freecad_reviewed_artifact_inputs import (
    MAX_REVIEWED_ARTIFACT_BYTES,
    ReviewedArtifactInputError,
    ReviewedArtifactInputErrorCode,
)
from vibecad.intent_bridge.freecad_imageplane_adapter import (
    IMAGEPLANE_OPERATION_SPEC,
    build_imageplane_artifact_document,
)
from vibecad.intent_bridge.freecad_part_file_import_adapter import (
    build_part_file_import_artifact_document,
)
from vibecad.parametric.freecad_imageplane_rules import HostOwnedImageStager
from vibecad.parametric.freecad_part_file_import_rules import (
    PART_FILE_IMPORT_NATIVE_SPECS,
    HostOwnedImportStager,
    PartFileImportConformanceReceipt,
    PartFileImportExecutionBindings,
    PartFileImportOperation,
)
from vibecad.parametric.freecad_planar_mechanical_rules import (
    PlanarMechanicalExecutionBindings,
    decode_planar_mechanical_plan,
)
from vibecad.workflow.contracts import AcceptanceSpec, ModelProgram
from vibecad.workflow.program import validate_model_program
from vibecad.workflow.reviewed_intent import ReviewedIntentProgramV1


def _program(intent: ReviewedIntentProgramV1 | None = None):
    selected = reviewed_box_program() if intent is None else intent
    return validate_model_program(
        ModelProgram(
            task_id="task_artifact_chain_integration",
            base_revision=BASE_REVISION,
            operations=(
                _command(
                    "artifact",
                    "apply_reviewed_intent",
                    args={"intent": selected.to_mapping()},
                ),
            ),
            acceptance=AcceptanceSpec(id="accept_artifact_chain_integration", criteria=()),
        )
    )


def _public_import_intent(
    operation: PartFileImportOperation,
    artifact_content_sha256: str,
) -> ReviewedIntentProgramV1:
    route = next(
        item
        for item in reviewed_execution.REVIEWED_PART_FILE_IMPORT_ROUTES
        if item.operation.operation_id == operation.value
    )
    graph = import_graph(operation, artifact_content_sha256)
    return ReviewedIntentProgramV1(
        operation_id=route.operation_id,
        semantic_operation=route.semantic_operation,
        intent_graph_sha256=graph.graph_sha256,
        intent_content_sha256=hashlib.sha256(graph.canonical_bytes).hexdigest(),
        intent_graph=graph,
    )


def _public_image_intent(
    artifact_content_sha256: str,
    *,
    x_size_mm: float,
) -> ReviewedIntentProgramV1:
    route = reviewed_execution.REVIEWED_IMAGEPLANE_ROUTES[0]
    graph = image_graph(
        artifact_content_sha256,
        configuration=image_configuration(x_size_mm=x_size_mm),
    )
    return ReviewedIntentProgramV1(
        operation_id=route.operation_id,
        semantic_operation=route.semantic_operation,
        intent_graph_sha256=graph.graph_sha256,
        intent_content_sha256=hashlib.sha256(graph.canonical_bytes).hexdigest(),
        intent_graph=graph,
    )


def _image_program(
    create: ReviewedIntentProgramV1,
    edit: ReviewedIntentProgramV1,
    *,
    include_source: bool = True,
):
    edit_args: dict[str, object] = {"intent": edit.to_mapping()}
    if include_source:
        edit_args["sources"] = ({"command_id": "create", "slot": "object"},)
    return validate_model_program(
        ModelProgram(
            task_id="task_imageplane_place_or_edit",
            base_revision=BASE_REVISION,
            operations=(
                _command(
                    "create",
                    "apply_reviewed_intent",
                    args={"intent": create.to_mapping()},
                ),
                _command(
                    "edit",
                    "apply_reviewed_intent",
                    args=edit_args,
                    depends_on=("create",),
                ),
            ),
            acceptance=AcceptanceSpec(id="accept_imageplane_place_or_edit", criteria=()),
        )
    )


def _managed_image_case(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    create_x: float = 80.0,
    edit_x: float = 120.0,
    include_source: bool = True,
):
    payload = image_fakes._IMAGE  # noqa: SLF001
    artifact = build_imageplane_artifact_document(payload, media_type="image/png")
    create = _public_image_intent(artifact.content_sha256, x_size_mm=create_x)
    edit = _public_image_intent(artifact.content_sha256, x_size_mm=edit_x)
    staging = tmp_path / "staging"
    staging.mkdir(mode=0o700, parents=True)
    staging.chmod(0o700)
    if sys.platform == "win32":
        _file_compat.set_private_dacl(staging)
    token = object()
    resolver, source, stagers = _resolver(
        artifact=artifact,
        family_id="freecad_imageplane",
        operation_id=IMAGEPLANE_OPERATION_SPEC.operation_id,
        payload=payload,
        stager=HostOwnedImageStager(staging),
        token=token,
    )
    session = _FakeSession()
    session.doc.TransientDir = ""
    session.doc.FileName = ""
    assets_root = tmp_path / "assets"
    assets_root.mkdir(mode=0o700, parents=True)
    assets_root.chmod(0o700)
    if sys.platform == "win32":
        _file_compat.set_private_dacl(assets_root)
    assets = DocumentAssetWorkspace(assets_root)
    assets.attach(session.doc)
    session._document_assets = assets  # noqa: SLF001
    monkeypatch.setitem(sys.modules, "FreeCAD", ModuleType("FreeCAD"))
    monkeypatch.setattr(
        reviewed_execution,
        "require_reviewed_route_verified",
        lambda route, *, freecad: None,
    )
    native_calls: list[object] = []

    def apply(*args: object, **kwargs: object):
        raw = args[0]
        plan = image_execution.decode_imageplane_backend_plan(
            raw,
            expected_content_sha256=kwargs["expected_content_sha256"],
            expected_plan_sha256=kwargs["expected_plan_sha256"],
        )
        native_calls.append(plan)
        return image_fakes._fake_native_apply(plan)(*args, **kwargs)  # noqa: SLF001

    monkeypatch.setattr(image_execution, "apply_imageplane_plan", apply)
    cursor = InProcessCadExecutor(store=_store())._prepare_program_execution(
        program=_image_program(create, edit, include_source=include_source),
        candidate=_active(session, tmp_path),
        artifact_resolver=resolver,
        artifact_run_token=token,
    )
    cursor._bind_run_resource(resolver)  # noqa: SLF001
    return cursor, session, source, stagers, native_calls


def _install_managed_import_apply(
    monkeypatch: pytest.MonkeyPatch,
    session: _FakeSession,
    artifact_size: int,
) -> list[PartFileImportExecutionBindings]:
    calls: list[PartFileImportExecutionBindings] = []

    def apply(
        raw: bytes,
        *,
        expected_content_sha256: str,
        expected_plan_sha256: str,
        bindings: PartFileImportExecutionBindings,
    ) -> PartFileImportConformanceReceipt:
        del expected_content_sha256
        assert bindings.document is session.doc
        calls.append(bindings)
        plan = import_execution.decode_part_file_import_backend_plan(
            raw,
            expected_plan_sha256=expected_plan_sha256,
        )
        name = f"Import_{plan.operation.value}"
        shape = _FakeShape(volume=0.0, shape_type="Compound", solid_count=0)
        result = SimpleNamespace(
            Document=session.doc,
            Name=name,
            TypeId=PART_FILE_IMPORT_NATIVE_SPECS[plan.operation].type_id,
            FileName="",
            State=("Up-to-date",),
            Shape=shape,
            Placement=_FakePlacement(0.0),
            isValid=lambda: True,
        )
        session.doc.Objects = (*session.doc.Objects, result)
        return PartFileImportConformanceReceipt(
            plan_sha256=plan.plan_sha256,
            operation=plan.operation,
            object_name=name,
            artifact_id=plan.artifact_id,
            artifact_content_sha256=plan.artifact_content_sha256,
            artifact_size_bytes=artifact_size,
            result_shape_type=shape.ShapeType,
            result_shape_sha256=hashlib.sha256(
                shape.exportBrepToString().encode("utf-8")
            ).hexdigest(),
            edge_count=len(shape.Edges),
            face_count=len(shape.Faces),
            solid_count=len(shape.Solids),
        )

    monkeypatch.setattr(import_execution, "apply_part_file_import_plan", apply)
    return calls


def _case(kind: str):
    if kind == "import":
        payload = b"worker-owned reviewed STEP"
        operation_id = PartFileImportOperation.STEP.value
        family = build_part_file_import_reviewed_family_descriptor()
        operation = next(
            item for item in family.manifest.operations if item.operation_id == operation_id
        )
        artifact = build_part_file_import_artifact_document(
            PartFileImportOperation.STEP,
            payload,
        )
    else:
        payload = image_fakes._IMAGE  # noqa: SLF001
        family = build_imageplane_reviewed_family_descriptor()
        operation = IMAGEPLANE_OPERATION_SPEC
        operation_id = operation.operation_id
        artifact = build_imageplane_artifact_document(payload, media_type="image/png")
    return family, operation, operation_id, artifact, payload


def test_registered_import_executes_from_public_model_program_with_exact_resolver(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    operation = PartFileImportOperation.STEP
    payload = b"public ModelProgram reviewed STEP"
    artifact = build_part_file_import_artifact_document(operation, payload)
    intent = _public_import_intent(operation, artifact.content_sha256)
    staging = tmp_path / "staging"
    staging.mkdir(mode=0o700)
    staging.chmod(0o700)
    if sys.platform == "win32":
        _file_compat.set_private_dacl(staging)
    token = object()
    resolver, source, stagers = _resolver(
        artifact=artifact,
        family_id="freecad_part_file_import",
        operation_id=operation.value,
        payload=payload,
        stager=HostOwnedImportStager(staging),
        token=token,
    )
    session = _FakeSession()
    native_calls = _install_managed_import_apply(monkeypatch, session, len(payload))
    monkeypatch.setitem(sys.modules, "FreeCAD", ModuleType("FreeCAD"))
    monkeypatch.setattr(
        reviewed_execution,
        "require_reviewed_route_verified",
        lambda route, *, freecad: None,
    )
    cursor = InProcessCadExecutor(store=_store())._prepare_program_execution(
        program=_program(intent),
        candidate=_active(session, tmp_path),
        artifact_resolver=resolver,
        artifact_run_token=token,
    )
    cursor._bind_run_resource(resolver)  # noqa: SLF001

    outcome = cursor.step()

    assert outcome.result.ok is True
    assert outcome.result.value["reviewed_operation_id"] == ("freecad_part_file_import.step")
    assert tuple(item.TypeId for item in session.doc.Objects) == ("Part::ImportStep",)
    assert len(native_calls) == 1
    assert source.reads == 1 and source.closed is True
    assert stagers.creates == [("freecad_part_file_import", "step")]
    assert stagers.closed is True
    assert tuple(staging.iterdir()) == ()


def test_registered_import_without_resolver_fails_before_native_mutation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    operation = PartFileImportOperation.STEP
    payload = b"public ModelProgram missing resolver"
    artifact = build_part_file_import_artifact_document(operation, payload)
    intent = _public_import_intent(operation, artifact.content_sha256)
    session = _FakeSession()
    native_calls = _install_managed_import_apply(monkeypatch, session, len(payload))
    cursor = InProcessCadExecutor(store=_store())._prepare_program_execution(
        program=_program(intent),
        candidate=_active(session, tmp_path),
    )

    outcome = cursor.step()

    assert outcome.result.ok is False
    assert native_calls == []
    assert tuple(session.doc.Objects) == ()


def _install_managed_pm1_apply(
    monkeypatch: pytest.MonkeyPatch,
    session: _FakeSession,
) -> list[object]:
    calls: list[object] = []

    def apply(
        raw: bytes,
        *,
        expected_content_sha256: str,
        expected_plan_sha256: str,
        bindings: PlanarMechanicalExecutionBindings,
    ):
        assert bindings.document is session.doc
        plan = decode_planar_mechanical_plan(
            raw,
            expected_content_sha256=expected_content_sha256,
            expected_plan_sha256=expected_plan_sha256,
        )
        calls.append(plan)
        before = tuple(session.doc.Objects)
        session.doc.Objects = list(before)
        try:
            receipt = pm_fakes._fake_apply(session.doc, plan)  # noqa: SLF001
            for item in session.doc.Objects:
                if not hasattr(item, "Placement"):
                    item.Placement = _FakePlacement(0.0)
                shape = getattr(item, "Shape", None)
                if shape is not None:
                    item.Shape = _FakeShape(
                        volume=float(shape.Volume),
                        solid_count=len(shape.Solids),
                        shape_type="Solid" if shape.Solids else "Wire",
                        wire_closed=True,
                    )
            return receipt
        finally:
            session.doc.Objects = tuple(session.doc.Objects)

    monkeypatch.setattr(pm_execution, "apply_planar_mechanical_plan", apply)
    return calls


@pytest.mark.parametrize(
    ("operation_id", "circle_count"),
    (("reference-profiles", 0), ("add", 1), ("remove", 1)),
)
def test_registered_pm1_executes_from_public_model_program_with_sealed_visual(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    operation_id: str,
    circle_count: int,
) -> None:
    intent = pm_fakes._reviewed_program(  # noqa: SLF001
        operation_id,
        circle_count=circle_count,
    )
    resolver, token = pm_fakes._reviewed_resolver(circle_count=circle_count)  # noqa: SLF001
    session = _FakeSession()
    session.doc.UndoMode = 1
    session.doc.HasPendingTransaction = False
    native_calls = _install_managed_pm1_apply(monkeypatch, session)
    monkeypatch.setitem(sys.modules, "FreeCAD", ModuleType("FreeCAD"))
    monkeypatch.setattr(
        reviewed_execution,
        "require_reviewed_route_verified",
        lambda route, *, freecad: None,
    )
    cursor = InProcessCadExecutor(store=_store())._prepare_program_execution(
        program=_program(intent),
        candidate=_active(session, tmp_path),
        artifact_resolver=resolver,
        artifact_run_token=token,
    )
    cursor._bind_run_resource(resolver)  # noqa: SLF001

    outcome = cursor.step()

    assert outcome.result.ok is True
    assert outcome.result.value["reviewed_operation_id"] == intent.operation_id
    assert len(native_calls) == 1
    assert len(session.doc.Objects) == 11 + 2 * circle_count


def test_registered_pm1_without_sealed_visual_fails_before_native_mutation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    intent = pm_fakes._reviewed_program("add", circle_count=0)  # noqa: SLF001
    session = _FakeSession()
    session.doc.UndoMode = 1
    session.doc.HasPendingTransaction = False
    native_calls = _install_managed_pm1_apply(monkeypatch, session)
    cursor = InProcessCadExecutor(store=_store())._prepare_program_execution(
        program=_program(intent),
        candidate=_active(session, tmp_path),
    )

    outcome = cursor.step()

    assert outcome.result.ok is False
    assert native_calls == []
    assert tuple(session.doc.Objects) == ()


def test_registered_pm1_late_adoption_failure_rolls_back_the_whole_transaction(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    class FailingAttachSession(_FakeSession):
        def attach_object_identity(self, obj: object, identity: object) -> object:
            if len(self.attached_identities) == 4:
                raise RuntimeError("bounded PM1 late-adoption failure")
            return super().attach_object_identity(obj, identity)

    intent = pm_fakes._reviewed_program("add", circle_count=1)  # noqa: SLF001
    resolver, token = pm_fakes._reviewed_resolver(circle_count=1)  # noqa: SLF001
    session = FailingAttachSession()
    session.doc.UndoMode = 1
    session.doc.HasPendingTransaction = False
    native_calls = _install_managed_pm1_apply(monkeypatch, session)
    monkeypatch.setitem(sys.modules, "FreeCAD", ModuleType("FreeCAD"))
    monkeypatch.setattr(
        reviewed_execution,
        "require_reviewed_route_verified",
        lambda route, *, freecad: None,
    )
    cursor = InProcessCadExecutor(store=_store())._prepare_program_execution(
        program=_program(intent),
        candidate=_active(session, tmp_path),
        artifact_resolver=resolver,
        artifact_run_token=token,
    )
    cursor._bind_run_resource(resolver)  # noqa: SLF001

    outcome = cursor.step()

    assert outcome.result.ok is False
    assert len(native_calls) == 1
    assert tuple(session.doc.Objects) == ()
    assert session.attached_identities == []
    assert session.result_object is None


def test_registered_imageplane_public_create_then_same_run_edit_preserves_identity(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    cursor, session, source, stagers, native_calls = _managed_image_case(
        monkeypatch,
        tmp_path,
    )

    created = cursor.step()
    edited = cursor.step()

    assert created.result.ok is True and edited.result.ok is True
    assert created.result.value["object_id"] == edited.result.value["object_id"]
    assert created.result.value["feature_id"] == edited.result.value["feature_id"]
    assert tuple(item.TypeId for item in session.doc.Objects) == ("Image::ImagePlane",)
    assert session.doc.Objects[0].XSize == 120.0
    assert len(native_calls) == 2
    assert source.reads == 2 and source.closed is True
    assert stagers.creates == [
        ("freecad_imageplane", "place_or_edit_image_plane"),
        ("freecad_imageplane", "place_or_edit_image_plane"),
    ]
    assert stagers.closed is True


@pytest.mark.parametrize("failure", ("missing_source", "stale", "tamper", "noop"))
def test_registered_imageplane_public_rejects_invalid_edit_without_new_state(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    failure: str,
) -> None:
    cursor, session, _source, _stagers, native_calls = _managed_image_case(
        monkeypatch,
        tmp_path,
        edit_x=80.0 if failure == "noop" else 120.0,
        include_source=failure != "missing_source",
    )
    created = cursor.step()
    assert created.result.ok is True
    feature = session.doc.Objects[0]
    retained = Path(feature.ImageFile)
    before_manifest = image_execution.imageplane_rules._workspace_manifest(  # noqa: SLF001
        Path(session.doc.TransientDir)
    )
    if failure == "stale":
        feature.XSize = 81.0
    elif failure == "tamper":
        retained.write_bytes(b"tampered")
        retained.chmod(0o600)

    rejected = cursor.step()

    assert rejected.result.ok is False
    assert session.doc.Objects == (feature,)
    if failure in {"missing_source", "noop"}:
        assert feature.XSize == 80.0
    elif failure == "stale":
        assert feature.XSize == 81.0
    else:
        assert retained.read_bytes() == b"tampered"
    expected_calls = 2 if failure == "noop" else 1
    assert len(native_calls) == expected_calls
    if failure != "tamper":
        assert (
            image_execution.imageplane_rules._workspace_manifest(  # noqa: SLF001
                Path(session.doc.TransientDir)
            )
            == before_manifest
        )


def test_registered_imageplane_late_adoption_failure_restores_object_and_workspace(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    original = image_execution.ImagePlaneOwnershipClosure.validate_adoption

    def reject_update(self, document, result, observation):
        if self.expected_disposition == "updated":
            raise RuntimeError("synthetic late adoption failure")
        return original(self, document, result, observation)

    monkeypatch.setattr(
        image_execution.ImagePlaneOwnershipClosure,
        "validate_adoption",
        reject_update,
    )
    cursor, session, _source, _stagers, native_calls = _managed_image_case(
        monkeypatch,
        tmp_path,
    )
    created = cursor.step()
    assert created.result.ok is True
    feature = session.doc.Objects[0]
    before_image_file = feature.ImageFile
    before_manifest = image_execution.imageplane_rules._workspace_manifest(  # noqa: SLF001
        Path(session.doc.TransientDir)
    )

    rejected = cursor.step()

    assert rejected.result.ok is False
    assert session.doc.Objects == (feature,)
    assert feature.XSize == 80.0
    assert feature.ImageFile == before_image_file
    assert (
        image_execution.imageplane_rules._workspace_manifest(  # noqa: SLF001
            Path(session.doc.TransientDir)
        )
        == before_manifest
    )
    assert len(native_calls) == 2


def test_registered_imageplane_without_artifact_resolver_is_pre_mutation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    payload = image_fakes._IMAGE  # noqa: SLF001
    artifact = build_imageplane_artifact_document(payload, media_type="image/png")
    intent = _public_image_intent(artifact.content_sha256, x_size_mm=80.0)
    session = _FakeSession()
    native_calls: list[object] = []
    monkeypatch.setattr(
        image_execution,
        "apply_imageplane_plan",
        lambda *args, **kwargs: native_calls.append((args, kwargs)),
    )
    cursor = InProcessCadExecutor(store=_store())._prepare_program_execution(
        program=_program(intent),
        candidate=_active(session, tmp_path),
    )

    outcome = cursor.step()

    assert outcome.result.ok is False
    assert native_calls == []
    assert session.doc.Objects == ()


@pytest.mark.parametrize("kind", ("import", "image"))
def test_executor_preparation_delivers_authority_only_to_artifact_family(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    kind: str,
) -> None:
    family, operation, operation_id, artifact, payload = _case(kind)
    route = _unregistered_route(family, operation)
    token = object()
    resolver, source, stagers = _resolver(
        artifact=artifact,
        family_id=family.manifest.family_id,
        operation_id=operation_id,
        payload=payload,
        stager=object(),
        token=token,
    )
    contexts: list[object] = []

    def execute_artifact(_session, _value, **private):
        assert private == {
            "_reviewed_artifact_resolver": resolver,
            "_reviewed_artifact_run_token": token,
        }
        resolution = resolver.resolve(
            run_token=token,
            family_id=family.manifest.family_id,
            operation_id=operation_id,
            artifact_id=artifact.artifact_id,
            content_sha256=artifact.content_sha256,
            role_term_ref_id=artifact.role_term_ref_id,
            schema_term_ref_id=artifact.schema_term_ref_id,
            media_type=artifact.media_type,
            maximum_bytes=MAX_REVIEWED_ARTIFACT_BYTES,
        )
        context = resolution.artifact_context
        assert (
            context.artifacts.read(context.artifact_document, MAX_REVIEWED_ARTIFACT_BYTES)
            == payload
        )
        contexts.append(context)
        raise RuntimeError("synthetic native failure after family context")

    monkeypatch.setattr(executor_module, "_route_reviewed_intent", lambda _value: route)
    monkeypatch.setattr(executor_module, "_execute_reviewed_intent_native", execute_artifact)
    cursor = InProcessCadExecutor(store=_store())._prepare_program_execution(
        program=_program(),
        candidate=_active(_FakeSession(), tmp_path),
        artifact_resolver=resolver,
        artifact_run_token=token,
    )
    cursor._bind_run_resource(resolver)  # noqa: SLF001

    outcome = cursor.step()

    assert outcome.result.ok is False
    assert cursor.done is True
    assert len(contexts) == 1 and contexts[0].artifact_document == artifact
    assert source.reads == 1 and source.closed is True
    assert stagers.creates == [] and stagers.closed is True
    with pytest.raises(ReviewedArtifactInputError) as closed:
        resolver.resolve(
            run_token=token,
            family_id=family.manifest.family_id,
            operation_id=operation_id,
            artifact_id=artifact.artifact_id,
            content_sha256=artifact.content_sha256,
            role_term_ref_id=artifact.role_term_ref_id,
            schema_term_ref_id=artifact.schema_term_ref_id,
            media_type=artifact.media_type,
            maximum_bytes=MAX_REVIEWED_ARTIFACT_BYTES,
        )
    assert closed.value.code is ReviewedArtifactInputErrorCode.CLOSED


def test_nonartifact_route_never_receives_or_calls_run_resolver(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    family, _operation, operation_id, artifact, payload = _case("import")
    token = object()
    resolver, source, stagers = _resolver(
        artifact=artifact,
        family_id=family.manifest.family_id,
        operation_id=operation_id,
        payload=payload,
        stager=object(),
        token=token,
    )
    received: list[dict[str, object]] = []

    def execute_nonartifact(_session, _value, **private):
        received.append(private)
        raise RuntimeError("stop after private-argument inspection")

    monkeypatch.setattr(executor_module, "_execute_reviewed_intent_native", execute_nonartifact)
    cursor = InProcessCadExecutor(store=_store())._prepare_program_execution(
        program=_program(),
        candidate=_active(_FakeSession(), tmp_path),
        artifact_resolver=resolver,
        artifact_run_token=token,
    )
    cursor._bind_run_resource(resolver)  # noqa: SLF001

    outcome = cursor.step()

    assert outcome.result.ok is False
    assert received == [{}]
    assert source.reads == 0 and source.closed is True
    assert stagers.creates == [] and stagers.closed is True
