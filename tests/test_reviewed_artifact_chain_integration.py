"""Program-run integration for registered Import and withheld Image artifacts."""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

import vibecad.execution.executor as executor_module
import vibecad.execution.freecad_part_file_import_reviewed_execution as import_execution
import vibecad.execution.freecad_reviewed_intent_execution as reviewed_execution
from tests import test_execution_freecad_imageplane_reviewed_execution as image_fakes
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
from vibecad.parametric.freecad_part_file_import_rules import (
    PART_FILE_IMPORT_NATIVE_SPECS,
    HostOwnedImportStager,
    PartFileImportConformanceReceipt,
    PartFileImportExecutionBindings,
    PartFileImportOperation,
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
            "_reviewed_run_token": token,
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
