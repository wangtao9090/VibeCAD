"""Private program-run integration for unregistered Reviewed artifact families."""

from __future__ import annotations

from pathlib import Path

import pytest

import vibecad.execution.executor as executor_module
from tests import test_execution_freecad_imageplane_reviewed_execution as image_fakes
from tests.test_program_executor import (
    BASE_REVISION,
    _active,
    _command,
    _FakeSession,
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
from vibecad.parametric.freecad_part_file_import_rules import PartFileImportOperation
from vibecad.workflow.contracts import AcceptanceSpec, ModelProgram
from vibecad.workflow.program import validate_model_program


def _program():
    return validate_model_program(
        ModelProgram(
            task_id="task_artifact_chain_integration",
            base_revision=BASE_REVISION,
            operations=(
                _command(
                    "artifact",
                    "apply_reviewed_intent",
                    args={"intent": reviewed_box_program().to_mapping()},
                ),
            ),
            acceptance=AcceptanceSpec(id="accept_artifact_chain_integration", criteria=()),
        )
    )


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
