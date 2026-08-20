"""Default-composition G2 integration for trusted reviewed task inputs."""

from __future__ import annotations

import hashlib
import os
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

import vibecad.execution.freecad_imageplane_reviewed_execution as image_execution
import vibecad.execution.freecad_part_file_import_reviewed_execution as import_execution
import vibecad.execution.freecad_reviewed_intent_execution as reviewed_execution
import vibecad.execution.worker_port as worker_port_module
import vibecad.worker.service as worker_service_module
from tests import test_execution_freecad_imageplane_reviewed_execution as image_fakes
from tests.test_intent_bridge_freecad_imageplane_adapter import (
    _configuration as image_configuration,
)
from tests.test_intent_bridge_freecad_imageplane_adapter import _graph as image_graph
from tests.test_intent_bridge_freecad_part_file_import_adapter import (
    _graph as import_graph,
)
from tests.test_program_executor import _FakeSession
from tests.test_reviewed_artifact_chain_integration import (
    _install_managed_import_apply,
)
from vibecad import _file_compat
from vibecad.application.agent import AgentApplication
from vibecad.application.public_surface import public_tool_specs
from vibecad.application.reviewed_input_ingress import (
    REVIEWED_INPUT_CATALOG_DIRECTORY,
    ReviewedInputKind,
    TrustedReviewedInputBytes,
    TrustedReviewedInputDescriptor,
)
from vibecad.engine.document_assets import DocumentAssetWorkspace
from vibecad.execution.candidate import ActiveCandidate, SessionBinding
from vibecad.execution.errors import ExecutorError, ExecutorErrorCode
from vibecad.execution.freecad_reviewed_intent_execution import (
    CURRENT_REVIEWED_INTENT_ROUTES,
)
from vibecad.execution.results import NormalizedToolOutcome, ToolDiagnosticClass
from vibecad.parametric.freecad_part_file_import_rules import PartFileImportOperation
from vibecad.workflow.contracts import (
    AcceptanceSpec,
    ModelCommand,
    ModelProgram,
    StepResult,
    ValueSource,
)
from vibecad.workflow.program import ValidatedProgram, validate_model_program
from vibecad.workflow.reviewed_intent import ReviewedIntentProgramV1
from vibecad.workflow.state import ReasoningOwner, ReviewPolicy, TaskStatus

_STEP = b"ISO-10303-21;\nHEADER;\nENDSEC;\nDATA;\nENDSEC;\nEND-ISO-10303-21;\n"
_PNG = (
    Path(__file__).parent / "fixtures" / "guided_photo_v1" / "images" / "calibration_block.png"
).read_bytes()


def _descriptor(kind: ReviewedInputKind, payload: bytes) -> TrustedReviewedInputDescriptor:
    return TrustedReviewedInputDescriptor(
        kind=kind,
        content_sha256=hashlib.sha256(payload).hexdigest(),
        size_bytes=len(payload),
    )


def _command(
    command_id: str,
    intent: object,
    *,
    depends_on: tuple[str, ...] = (),
    sources: tuple[dict[str, str], ...] | None = None,
) -> ModelCommand:
    args: dict[str, object] = {"intent": intent.to_mapping()}
    if sources is not None:
        args["sources"] = sources
    return ModelCommand(
        id=command_id,
        op="apply_reviewed_intent",
        target={},
        args=args,
        depends_on=depends_on,
        source=ValueSource.MODEL,
    )


def _import_intent(
    artifact_content_sha256: str,
    artifact_id: str,
) -> ReviewedIntentProgramV1:
    route = next(
        item
        for item in reviewed_execution.REVIEWED_PART_FILE_IMPORT_ROUTES
        if item.operation.operation_id == PartFileImportOperation.STEP.value
    )
    graph = import_graph(
        PartFileImportOperation.STEP,
        artifact_content_sha256,
        artifact_id=artifact_id,
    )
    return ReviewedIntentProgramV1(
        operation_id=route.operation_id,
        semantic_operation=route.semantic_operation,
        intent_graph_sha256=graph.graph_sha256,
        intent_content_sha256=hashlib.sha256(graph.canonical_bytes).hexdigest(),
        intent_graph=graph,
    )


def _image_intent(
    artifact_content_sha256: str,
    artifact_id: str,
    *,
    x_size_mm: float,
) -> ReviewedIntentProgramV1:
    route = reviewed_execution.REVIEWED_IMAGEPLANE_ROUTES[0]
    graph = image_graph(
        artifact_content_sha256,
        artifact_id=artifact_id,
        configuration=image_configuration(x_size_mm=x_size_mm),
    )
    return ReviewedIntentProgramV1(
        operation_id=route.operation_id,
        semantic_operation=route.semantic_operation,
        intent_graph_sha256=graph.graph_sha256,
        intent_content_sha256=hashlib.sha256(graph.canonical_bytes).hexdigest(),
        intent_graph=graph,
    )


def _program(
    *,
    task_id: str,
    base_revision: str,
    import_digest: str,
    import_artifact_id: str,
    image_artifact_id: str,
    include_images: bool,
) -> ValidatedProgram:
    operations = [
        _command(
            "import",
            _import_intent(import_digest, import_artifact_id),
        )
    ]
    if include_images:
        image_digest = hashlib.sha256(_PNG).hexdigest()
        operations.extend(
            (
                _command(
                    "image_create",
                    _image_intent(
                        image_digest,
                        image_artifact_id,
                        x_size_mm=80.0,
                    ),
                ),
                _command(
                    "image_edit",
                    _image_intent(
                        image_digest,
                        image_artifact_id,
                        x_size_mm=120.0,
                    ),
                    depends_on=("image_create",),
                    sources=({"command_id": "image_create", "slot": "object"},),
                ),
            )
        )
    return validate_model_program(
        ModelProgram(
            task_id=task_id,
            base_revision=base_revision,
            operations=tuple(operations),
            acceptance=AcceptanceSpec(id="accept_reviewed_input_g2", criteria=()),
        )
    )


class _ProgramBeginWorker:
    """In-process worker wire peer using the real ``program.begin`` service path."""

    def __init__(self, session: _FakeSession) -> None:
        self.session = session
        self.begin_envelopes: list[dict[str, object] | None] = []
        self.snapshot_entries: list[tuple[str, ...]] = []
        self.close_calls = 0

    def execute_program(
        self,
        *,
        program: ValidatedProgram,
        candidate: object,
        session: object,
        artifact_snapshot: dict[str, object] | None = None,
        artifact_snapshot_fd: int | None = None,
        artifact_snapshot_capability: dict[str, object] | None = None,
    ) -> tuple[NormalizedToolOutcome, ...]:
        assert session is self.session
        if sys.platform == "win32":
            assert (artifact_snapshot is None) == (artifact_snapshot_capability is None)
            assert artifact_snapshot_fd is None
        else:
            assert (artifact_snapshot is None) == (artifact_snapshot_fd is None)
            assert artifact_snapshot_capability is None
        service = worker_service_module.WorkerService(
            "worker_generation_0123456789abcdef0123456789abcdef"
        )
        candidate_id = "worker_candidate_0123456789abcdef0123456789abcdef"
        session_id = "worker_session_0123456789abcdef0123456789abcdef"
        directory_capability = None
        if sys.platform == "win32":
            candidate_root = candidate.model_path.parent / "worker-candidate"
            candidate_root.mkdir(parents=True, exist_ok=True)
            _file_compat.set_private_dacl(candidate_root)
            model_path = candidate_root / "model.FCStd"
            step_path = candidate_root / "model.step"
            model_path.write_bytes(b"candidate-model")
            step_path.write_bytes(b"candidate-step")
            _file_compat.set_private_dacl(model_path)
            _file_compat.set_private_dacl(step_path)
            directory_capability = _file_compat.capture_windows_path(
                candidate_root,
                directory=True,
            )
        service._candidates[candidate_id] = SimpleNamespace(  # noqa: SLF001
            candidate_id=candidate_id,
            project_id=candidate.project_id,
            revision_id=candidate.revision_id,
            base_revision_id=candidate.base_revision,
            directory_capability=directory_capability,
        )
        service._sessions[session_id] = SimpleNamespace(  # noqa: SLF001
            session_id=session_id,
            capability_kind="candidate",
            capability_id=candidate_id,
            value=self.session,
            freeform_digest=None,
        )
        params: dict[str, object] = {
            "session_id": session_id,
            "candidate_id": candidate_id,
            "program": program.program.to_mapping(),
        }
        descriptors: tuple[int, ...] = ()
        if artifact_snapshot is not None:
            params["artifact_snapshot"] = artifact_snapshot
            if sys.platform == "win32":
                assert artifact_snapshot_capability is not None
                params["artifact_path_capability"] = artifact_snapshot_capability
                root = Path(artifact_snapshot_capability["path"])
                self.snapshot_entries.append(tuple(sorted(path.name for path in root.iterdir())))
            else:
                assert artifact_snapshot_fd is not None
                os.fstat(artifact_snapshot_fd)
                descriptors = (artifact_snapshot_fd,)
                self.snapshot_entries.append(tuple(sorted(os.listdir(artifact_snapshot_fd))))
        self.begin_envelopes.append(artifact_snapshot)
        begin = service.dispatch("program.begin", params, descriptors)
        outcomes: list[NormalizedToolOutcome] = []
        for index, _command_value in enumerate(program.commands):
            response = service.dispatch(
                "program.execute_command",
                {"program_id": begin["program_id"], "index": index},
                (),
            )
            raw = response["outcome"]
            diagnostic = raw["diagnostic"]
            outcome = NormalizedToolOutcome(
                result=StepResult.from_mapping(raw["result"]),
                diagnostic=None if diagnostic is None else ToolDiagnosticClass(diagnostic),
            )
            outcomes.append(outcome)
            if not outcome.result.ok:
                break
        assert service._programs == {}  # noqa: SLF001
        return tuple(outcomes)

    def close(self) -> None:
        self.close_calls += 1


def _install_candidate(
    port: object,
    worker: _ProgramBeginWorker,
    *,
    project_id: str,
    base_head: object,
    session: _FakeSession,
    tmp_path: Path,
) -> ActiveCandidate:
    revision_id = "revision_11111111111111111111111111111111"
    active = ActiveCandidate(
        project_id=project_id,
        base_head=base_head,
        binding=SessionBinding(
            project_id=project_id,
            revision_id=revision_id,
            session=session,
        ),
        model_path=tmp_path / "candidate.FCStd",
        step_path=tmp_path / "candidate.step",
    )
    capability = SimpleNamespace(
        project_id=project_id,
        revision_id=revision_id,
        base_revision=base_head.revision_id,
        model_path=active.model_path,
        step_path=active.step_path,
    )
    state = worker_port_module._Capability(  # noqa: SLF001
        kind="candidate",
        key=(project_id, revision_id),
        value=capability,
        base_head=base_head,
    )
    state.sessions.add(session)
    port._worker = worker  # noqa: SLF001
    port._sessions[session] = state  # noqa: SLF001
    return active


def test_default_composition_executes_two_sealed_attachments_through_program_begin(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_root = tmp_path / "agent" / "data"
    application = AgentApplication.open(data_root=data_root)
    project = application.bootstrap_empty().head
    created = application.create_task(
        task_id="task_11111111111111111111111111111111",
        project_id=project.project_id,
        reasoning_owner=ReasoningOwner.EXTERNAL_PLAN,
        review_policy=ReviewPolicy.REQUIRE_REVIEW,
    )
    task = created.task_run
    assert task.status is TaskStatus.NEEDS_PLAN
    receipt = application.seal_reviewed_task_inputs(
        task_id=task.id,
        project_id=task.project_id,
        base_revision=task.base_revision,
        inputs=(
            TrustedReviewedInputBytes(
                descriptor=_descriptor(ReviewedInputKind.STEP, _STEP),
                payload=_STEP,
            ),
            TrustedReviewedInputBytes(
                descriptor=_descriptor(ReviewedInputKind.PNG, _PNG),
                payload=_PNG,
            ),
        ),
    )
    assert len(receipt.records) == 2
    import_record = next(
        record for record in receipt.records if record.family_id == "freecad_part_file_import"
    )
    image_record = next(
        record for record in receipt.records if record.family_id == "freecad_imageplane"
    )

    session = _FakeSession()
    session.doc.TransientDir = ""
    session.doc.FileName = ""
    assets_root = tmp_path / "document-assets"
    assets_root.mkdir(mode=0o700)
    assets_root.chmod(0o700)
    assets = DocumentAssetWorkspace(assets_root)
    workspace = assets.attach(session.doc)
    session._document_assets = assets  # noqa: SLF001
    worker = _ProgramBeginWorker(session)
    with application._cad_gate:  # noqa: SLF001
        port = application._cad_execution_port_under_gate()  # noqa: SLF001
    assert port._task_input_preflight is application._reviewed_inputs  # noqa: SLF001
    assert port._task_input_snapshot_provider is application._reviewed_inputs  # noqa: SLF001
    candidate = _install_candidate(
        port,
        worker,
        project_id=task.project_id,
        base_head=project,
        session=session,
        tmp_path=tmp_path,
    )

    monkeypatch.setitem(sys.modules, "FreeCAD", ModuleType("FreeCAD"))
    monkeypatch.setattr(
        reviewed_execution,
        "require_reviewed_route_verified",
        lambda route, *, freecad: None,
    )
    import_calls = _install_managed_import_apply(monkeypatch, session, len(_STEP))
    original_import_apply = import_execution.apply_part_file_import_plan

    def apply_import(*args: object, **kwargs: object):
        receipt = original_import_apply(*args, **kwargs)
        for item in session.doc.Objects:
            if not hasattr(item, "PropertiesList"):
                item.PropertiesList = ()
        return receipt

    monkeypatch.setattr(import_execution, "apply_part_file_import_plan", apply_import)
    image_calls: list[object] = []

    def apply_image(*args: object, **kwargs: object):
        plan = image_execution.decode_imageplane_backend_plan(
            args[0],
            expected_content_sha256=kwargs["expected_content_sha256"],
            expected_plan_sha256=kwargs["expected_plan_sha256"],
        )
        image_calls.append(plan)
        return image_fakes._fake_native_apply(plan, _PNG)(*args, **kwargs)  # noqa: SLF001

    monkeypatch.setattr(image_execution, "apply_imageplane_plan", apply_image)
    stager_roots: list[Path] = []
    original_stager_factory = worker_service_module._WorkerArtifactStagerFactory  # noqa: SLF001

    def tracked_stager_factory():
        factory = original_stager_factory()
        stager_roots.append(factory._root)  # noqa: SLF001
        return factory

    monkeypatch.setattr(
        worker_service_module,
        "_WorkerArtifactStagerFactory",
        tracked_stager_factory,
    )
    program = _program(
        task_id=task.id,
        base_revision=task.base_revision,
        import_digest=hashlib.sha256(_STEP).hexdigest(),
        import_artifact_id=import_record.artifact_id,
        image_artifact_id=image_record.artifact_id,
        include_images=True,
    )

    outcomes = port.execute_program(program=program, candidate=candidate)

    assert tuple(item.result.ok for item in outcomes) == (True, True, True), tuple(
        item.result.to_mapping() for item in outcomes
    )
    assert outcomes[1].result.value["object_id"] == outcomes[2].result.value["object_id"]
    assert outcomes[1].result.value["feature_id"] == outcomes[2].result.value["feature_id"]
    assert tuple(item.TypeId for item in session.doc.Objects) == (
        "Part::ImportStep",
        "Image::ImagePlane",
    )
    assert session.doc.Objects[1].XSize == 120.0
    assert len(import_calls) == 1 and len(image_calls) == 2
    assert len(worker.begin_envelopes) == 1 and worker.begin_envelopes[0] is not None
    assert worker.snapshot_entries == [
        tuple(sorted(("manifest.json", *(record.artifact_id for record in receipt.records))))
    ]
    assert all(not root.exists() for root in stager_roots)
    catalog_root = data_root / REVIEWED_INPUT_CATALOG_DIRECTORY
    assert not tuple(path for path in catalog_root.iterdir() if path.name.startswith(".run_"))

    before_objects = session.doc.Objects
    before_native_calls = (len(import_calls), len(image_calls))
    negative_programs = (
        _program(
            task_id="task_22222222222222222222222222222222",
            base_revision=task.base_revision,
            import_digest=hashlib.sha256(_STEP).hexdigest(),
            import_artifact_id=import_record.artifact_id,
            image_artifact_id=image_record.artifact_id,
            include_images=False,
        ),
        _program(
            task_id=task.id,
            base_revision=task.base_revision,
            import_digest=hashlib.sha256(b"not the sealed STEP").hexdigest(),
            import_artifact_id=import_record.artifact_id,
            image_artifact_id=image_record.artifact_id,
            include_images=False,
        ),
    )
    for rejected in negative_programs:
        rejected_outcomes = port.execute_program(program=rejected, candidate=candidate)
        assert rejected_outcomes[0].result.ok is False
        assert session.doc.Objects == before_objects
        assert (len(import_calls), len(image_calls)) == before_native_calls

    wrong_base = _program(
        task_id=task.id,
        base_revision="revision_22222222222222222222222222222222",
        import_digest=hashlib.sha256(_STEP).hexdigest(),
        import_artifact_id=import_record.artifact_id,
        image_artifact_id=image_record.artifact_id,
        include_images=False,
    )
    with pytest.raises(ExecutorError) as rejected_base:
        port.execute_program(program=wrong_base, candidate=candidate)
    assert rejected_base.value.code is ExecutorErrorCode.INVALID_CANDIDATE
    assert session.doc.Objects == before_objects
    assert (len(import_calls), len(image_calls)) == before_native_calls
    assert all(not root.exists() for root in stager_roots)
    assert not tuple(path for path in catalog_root.iterdir() if path.name.startswith(".run_"))

    assets.release_after_close(session.doc)
    assert not workspace.exists() and tuple(assets_root.iterdir()) == ()
    application.discard_reviewed_task_inputs(
        task_id=task.id,
        project_id=task.project_id,
        base_revision=task.base_revision,
    )
    assert tuple(catalog_root.iterdir()) == ()
    assert len(CURRENT_REVIEWED_INTENT_ROUTES) == 126
    assert len(public_tool_specs()) == 39
    application.close()
    assert worker.close_calls == 1
