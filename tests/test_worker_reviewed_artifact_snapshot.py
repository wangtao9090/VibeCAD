from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path

import pytest

import vibecad.execution.adapter as adapter_module
from tests.test_freecad_worker import (
    _GENERATION,
    _candidate_rig,
    _inspect_program,
    _process,
)
from vibecad._file_compat import (
    WindowsPathCapability,
    capture_windows_path,
    set_private_dacl,
    validate_windows_path,
)
from vibecad.execution.adapter import AdapterError
from vibecad.execution.freecad_reviewed_artifact_inputs import (
    MAX_REVIEWED_ARTIFACT_BYTES,
    ReviewedArtifactCatalogRecord,
    ReviewedArtifactCatalogSnapshot,
    ReviewedArtifactInputError,
    ReviewedArtifactInputErrorCode,
)
from vibecad.execution.registry import ExecutionProfile
from vibecad.execution.results import normalize_tool_result
from vibecad.intent_bridge.ports import read_verified_document
from vibecad.worker import FreeCadWorker
from vibecad.worker.generation import _WorkerProcess
from vibecad.worker.service import (
    WorkerService,
    _Candidate,
    _open_artifact_run_resolver,
    _Program,
    _ServiceError,
    _Session,
)
from vibecad.workflow.contracts import (
    AcceptanceSpec,
    ModelCommand,
    ModelProgram,
    ValueSource,
)
from vibecad.workflow.program import validate_model_program

_PAYLOAD = b"authenticated STEP bytes"
_DIGEST = hashlib.sha256(_PAYLOAD).hexdigest()
_TASK_ID = "task_" + "a" * 32
_PROJECT_ID = "project_" + "b" * 32
_BASE_REVISION = "revision_" + "c" * 32
_RUN_ID = "artifact_run_" + "d" * 32


class _Closer:
    def __init__(self) -> None:
        self.close_count = 0

    def close(self) -> None:
        self.close_count += 1


def _validated_inspect_program(count: int = 1):
    return validate_model_program(
        ModelProgram(
            task_id="task_adapter_resource",
            base_revision="revision_adapter_resource",
            operations=tuple(
                ModelCommand(
                    id=f"inspect-{index}",
                    op="inspect_model",
                    target={},
                    args={},
                    depends_on=(),
                    preserve=(),
                    source=ValueSource.MODEL,
                )
                for index in range(count)
            ),
            acceptance=AcceptanceSpec(id="acceptance_adapter_resource", criteria=()),
        )
    )


def _execution(handler, *, count: int = 1):
    return adapter_module._prepare_validated_program_execution(
        _validated_inspect_program(count),
        {"inspect_model": handler},
        execution_profile=ExecutionProfile.HEADLESS,
    )


def _artifact_record() -> ReviewedArtifactCatalogRecord:
    return ReviewedArtifactCatalogRecord(
        artifact_id="artifact_step",
        content_sha256=_DIGEST,
        size_bytes=len(_PAYLOAD),
        media_type="model/step",
        role_term_ref_id="role_part_file_import_artifact",
        schema_term_ref_id="schema_part_step_artifact_v1",
        document_id=f"part_file_import_{_DIGEST[:32]}",
        family_id="freecad_part_file_import",
        operation_ids=("step",),
        maximum_bytes=MAX_REVIEWED_ARTIFACT_BYTES,
    )


def _snapshot_directory(
    root: Path,
    *,
    task_id: str = _TASK_ID,
    project_id: str = _PROJECT_ID,
    base_revision: str = _BASE_REVISION,
) -> tuple[
    ReviewedArtifactCatalogSnapshot,
    Path,
    int,
    WindowsPathCapability | None,
    dict[str, object],
]:
    snapshot = ReviewedArtifactCatalogSnapshot(
        task_id=task_id,
        project_id=project_id,
        base_revision=base_revision,
        run_id=_RUN_ID,
        records=(_artifact_record(),),
    )
    directory = root / "artifact-snapshot"
    directory.mkdir(mode=0o700)
    directory.chmod(0o700)
    manifest = json.dumps(
        snapshot.to_mapping(),
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    for name, payload in (("manifest.json", manifest), ("artifact_step", _PAYLOAD)):
        path = directory / name
        path.write_bytes(payload)
        path.chmod(0o600)
        if sys.platform == "win32":
            set_private_dacl(path)
    capability: WindowsPathCapability | None = None
    if sys.platform == "win32":
        set_private_dacl(directory)
        descriptor = -1
        capability = capture_windows_path(directory, directory=True)
    else:
        descriptor = os.open(
            directory,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0),
        )
    envelope: dict[str, object] = {
        "kind": "reviewed_artifact_snapshot_v1",
        "schema_version": 1,
        "task_id": snapshot.task_id,
        "project_id": snapshot.project_id,
        "base_revision": snapshot.base_revision,
        "run_id": snapshot.run_id,
        "catalog_sha256": snapshot.catalog_sha256,
    }
    return snapshot, directory, descriptor, capability, envelope


def _open_snapshot_resolver(
    descriptor: int,
    capability: WindowsPathCapability | None,
    envelope: dict[str, object],
):
    return _open_artifact_run_resolver(
        descriptor,
        envelope,
        directory_capability=capability,
    )


def _assert_snapshot_authority_live(
    descriptor: int,
    capability: WindowsPathCapability | None,
) -> None:
    if capability is None:
        assert os.fstat(descriptor).st_mode
    else:
        validate_windows_path(capability, directory=True)


def _close_snapshot_authority(descriptor: int) -> None:
    if descriptor >= 0:
        os.close(descriptor)


def _resolve(resolver, token):
    return resolver.resolve(
        run_token=token,
        family_id="freecad_part_file_import",
        operation_id="step",
        artifact_id="artifact_step",
        content_sha256=_DIGEST,
        role_term_ref_id="role_part_file_import_artifact",
        schema_term_ref_id="schema_part_step_artifact_v1",
        media_type="model/step",
        maximum_bytes=MAX_REVIEWED_ARTIFACT_BYTES,
    )


def test_execution_cursor_closes_bound_run_resource_on_success_and_cancel() -> None:
    successful = _execution(lambda: {"valid": True})
    success_resource = _Closer()
    successful._bind_run_resource(success_resource)

    assert successful.step().result.ok is True
    assert successful.done is True
    assert success_resource.close_count == 1
    successful.close()
    assert success_resource.close_count == 1

    cancelled = _execution(lambda: {"valid": True})
    cancel_resource = _Closer()
    cancelled._bind_run_resource(cancel_resource)
    cancelled.close()

    assert cancelled.done is True
    assert cancelled.outcomes == ()
    assert cancel_resource.close_count == 1
    with pytest.raises(AdapterError):
        cancelled.step()


def test_execution_cursor_closes_on_first_failure_and_rejects_rebinding() -> None:
    def fail() -> object:
        raise RuntimeError("private handler detail")

    execution = _execution(fail, count=2)
    resource = _Closer()
    execution._bind_run_resource(resource)
    with pytest.raises(AdapterError):
        execution._bind_run_resource(_Closer())

    outcome = execution.step()

    assert outcome.result.ok is False
    assert execution.done is True
    assert len(execution.outcomes) == 1
    assert resource.close_count == 1


def test_worker_opens_exact_snapshot_and_keeps_original_authority_host_owned(
    tmp_path: Path,
) -> None:
    snapshot, _directory, descriptor, capability, envelope = _snapshot_directory(tmp_path)
    try:
        resolver, token = _open_snapshot_resolver(descriptor, capability, envelope)
        context = _resolve(resolver, token).artifact_context

        assert resolver.catalog_sha256 == snapshot.catalog_sha256
        assert (
            read_verified_document(
                context.artifacts,
                context.artifact_document,
                maximum_bytes=MAX_REVIEWED_ARTIFACT_BYTES,
            )
            == _PAYLOAD
        )
        _assert_snapshot_authority_live(descriptor, capability)

        resolver.close()
        resolver.close()
        _assert_snapshot_authority_live(descriptor, capability)
        with pytest.raises(ReviewedArtifactInputError) as closed:
            context.artifacts.read(
                context.artifact_document,
                MAX_REVIEWED_ARTIFACT_BYTES,
            )
        assert closed.value.code is ReviewedArtifactInputErrorCode.CLOSED
    finally:
        _close_snapshot_authority(descriptor)


@pytest.mark.parametrize("mutation", ("payload", "extra", "envelope"))
def test_worker_snapshot_tamper_fails_before_resolver_is_returned(
    mutation: str,
    tmp_path: Path,
) -> None:
    _snapshot, directory, descriptor, capability, envelope = _snapshot_directory(tmp_path)
    if mutation == "payload":
        (directory / "artifact_step").write_bytes(b"tampered")
        (directory / "artifact_step").chmod(0o600)
    elif mutation == "extra":
        (directory / "unexpected").write_bytes(b"unexpected")
        (directory / "unexpected").chmod(0o600)
        if sys.platform == "win32":
            set_private_dacl(directory / "unexpected")
    else:
        envelope["run_id"] = "artifact_run_other"
    try:
        with pytest.raises(_ServiceError) as failure:
            _open_snapshot_resolver(descriptor, capability, envelope)
        assert failure.value.code.value == "integrity_failure"
    finally:
        _close_snapshot_authority(descriptor)


def test_live_snapshot_mutation_is_rejected_after_begin(tmp_path: Path) -> None:
    _snapshot, directory, descriptor, capability, envelope = _snapshot_directory(tmp_path)
    resolver = None
    try:
        resolver, token = _open_snapshot_resolver(descriptor, capability, envelope)
        context = _resolve(resolver, token).artifact_context
        extra = directory / "late-entry"
        extra.write_bytes(b"late")
        extra.chmod(0o600)
        if sys.platform == "win32":
            set_private_dacl(extra)

        with pytest.raises(ReviewedArtifactInputError) as failure:
            context.artifacts.read(
                context.artifact_document,
                MAX_REVIEWED_ARTIFACT_BYTES,
            )
        assert failure.value.code is ReviewedArtifactInputErrorCode.INTEGRITY_FAILURE
    finally:
        if resolver is not None:
            resolver.close()
        _close_snapshot_authority(descriptor)


def test_program_begin_requires_snapshot_mapping_and_exact_platform_authority(
    tmp_path: Path,
) -> None:
    _snapshot, _directory, descriptor, capability, envelope = _snapshot_directory(tmp_path)
    service = WorkerService(_GENERATION)
    legacy = {"session_id": "x", "candidate_id": "y", "program": {}}
    artifact = {**legacy, "artifact_snapshot": envelope}
    boolean_schema = {
        **legacy,
        "artifact_snapshot": {**envelope, "schema_version": True},
    }
    try:
        if capability is None:
            invalid = (
                (legacy, (descriptor,)),
                (artifact, ()),
                (artifact, (descriptor, descriptor)),
                (boolean_schema, (descriptor,)),
            )
            ready_params: dict[str, object] = {}
            ready_descriptors = (descriptor,)
        else:
            capability_mapping = capability.to_mapping()
            authorized = {
                **artifact,
                "artifact_path_capability": capability_mapping,
            }
            invalid = (
                ({**legacy, "artifact_path_capability": capability_mapping}, ()),
                (artifact, ()),
                ({**authorized, "unexpected": True}, ()),
                (
                    {
                        **boolean_schema,
                        "artifact_path_capability": capability_mapping,
                    },
                    (),
                ),
            )
            ready_params = {"artifact_path_capability": capability_mapping}
            ready_descriptors = ()
        for params, descriptors in invalid:
            with pytest.raises(_ServiceError) as failure:
                service.dispatch("program.begin", params, descriptors)
            assert failure.value.code.value == "invalid_request"
        with pytest.raises(_ServiceError) as unexpected:
            service.dispatch("worker.ready", ready_params, ready_descriptors)
        assert unexpected.value.code.value == "invalid_request"
    finally:
        service.close()
        _close_snapshot_authority(descriptor)


@pytest.mark.parametrize("failure", (False, True), ids=("done", "failure"))
def test_program_begin_binds_resolver_to_real_cursor_until_done(
    tmp_path: Path,
    failure: bool,
) -> None:
    snapshot, _directory, descriptor, capability, envelope = _snapshot_directory(tmp_path)
    source = ModelProgram(
        task_id=snapshot.task_id,
        base_revision=snapshot.base_revision,
        operations=(
            ModelCommand(
                id="inspect",
                op="inspect_model",
                target={},
                args={},
                depends_on=(),
                preserve=(),
                source=ValueSource.MODEL,
            ),
        ),
        acceptance=AcceptanceSpec(id="acceptance_artifact_worker", criteria=()),
    )

    prepared_authority: list[tuple[object, object]] = []
    resolved_contexts: list[object] = []

    class Engine:
        def validate_program(self, program: ModelProgram):
            return validate_model_program(program)

        def _prepare_program_execution(
            self,
            *,
            program,
            candidate,
            artifact_resolver,
            artifact_run_token,
        ):
            del candidate

            def inspect_model():
                resolved_contexts.append(
                    _resolve(artifact_resolver, artifact_run_token).artifact_context
                )
                if failure:
                    raise RuntimeError("synthetic artifact-family failure")
                return {"valid": True}

            prepared_authority.append((artifact_resolver, artifact_run_token))
            return adapter_module._prepare_validated_program_execution(
                program,
                {"inspect_model": inspect_model},
                execution_profile=ExecutionProfile.HEADLESS,
            )

        def close(self, _session: object) -> None:
            return None

    service = WorkerService(_GENERATION)
    service._engine = Engine()  # type: ignore[assignment]
    candidate_id = "worker_candidate_" + "4" * 32
    session_id = "worker_session_" + "5" * 32
    service._candidates[candidate_id] = _Candidate(  # type: ignore[arg-type]
        candidate_id=candidate_id,
        project_id=snapshot.project_id,
        revision_id="revision_" + "e" * 32,
        base_revision_id=snapshot.base_revision,
        directory_fd=-1,
        directory_identity=None,
        model_identity=None,
        step_identity=None,
    )
    service._sessions[session_id] = _Session(
        session_id=session_id,
        capability_kind="candidate",
        capability_id=candidate_id,
        value=object(),
    )
    begin_params = {
        "session_id": session_id,
        "candidate_id": candidate_id,
        "program": source.to_mapping(),
        "artifact_snapshot": envelope,
    }
    if capability is not None:
        begin_params["artifact_path_capability"] = capability.to_mapping()
    try:
        begun = service.dispatch(
            "program.begin",
            begin_params,
            () if capability is not None else (descriptor,),
        )
        program_id = begun["program_id"]
        assert type(program_id) is str
        active = service._programs[program_id]
        assert active.artifact_resolver is not None
        assert active.artifact_run_token is not None
        assert prepared_authority == [
            (active.artifact_resolver, active.artifact_run_token),
        ]

        result = service.dispatch(
            "program.execute_command",
            {"program_id": program_id, "index": 0},
            (),
        )

        assert result["done"] is True
        assert result["outcome"]["result"]["ok"] is not failure
        assert program_id not in service._programs
        assert len(resolved_contexts) == 1
        context = resolved_contexts[0]
        with pytest.raises(ReviewedArtifactInputError) as closed:
            context.artifacts.read(
                context.artifact_document,
                MAX_REVIEWED_ARTIFACT_BYTES,
            )
        assert closed.value.code is ReviewedArtifactInputErrorCode.CLOSED
        _assert_snapshot_authority_live(descriptor, capability)
    finally:
        service.close()
        _close_snapshot_authority(descriptor)


@pytest.mark.parametrize("closure", ("session", "shutdown", "connection"))
def test_service_closes_program_resources_on_every_terminal_path(closure: str) -> None:
    service = WorkerService(_GENERATION)
    session_id = "worker_session_" + "1" * 32
    program_id = "worker_program_" + "2" * 32
    resource = _Closer()

    class Engine:
        def close(self, _session: object) -> None:
            return None

    service._engine = Engine()  # type: ignore[assignment]
    service._sessions[session_id] = _Session(
        session_id=session_id,
        capability_kind="candidate",
        capability_id="worker_candidate_" + "3" * 32,
        value=object(),
    )
    service._programs[program_id] = _Program(
        program_id=program_id,
        session_id=session_id,
        candidate_id="worker_candidate_" + "3" * 32,
        command_ids=("inspect",),
        deadlines_ms=(10_000,),
        execution=resource,
    )

    if closure == "shutdown":
        assert service.dispatch("worker.shutdown", {}, ()) == {"closed": True}
    elif closure == "session":
        assert service.dispatch("session.close", {"session_id": session_id}, ()) == {
            "session_id": session_id
        }
    else:
        service.close()

    assert resource.close_count == 1
    assert service._programs == {}
    service.close()
    assert resource.close_count == 1


def test_proxy_forwards_exact_snapshot_authority_without_invalidating_host_copy(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    process, _grandchild = _process(tmp_path, "proxy_idle")
    worker = FreeCadWorker(process)
    descriptor = -1
    snapshot_capability: WindowsPathCapability | None = None
    calls: list[tuple[dict[str, object], int | None]] = []
    original_request = _WorkerProcess.request

    def recording_request(
        self: _WorkerProcess,
        method: str,
        params: dict[str, object],
        *,
        timeout_ms: int,
        capability_fd: int | None = None,
    ) -> dict[str, object]:
        if method == "program.begin":
            calls.append((params, capability_fd))
        if method == "program.execute_command":
            outcome = normalize_tool_result(
                {"valid": True},
                operation_id="inspect",
                elapsed_ms=1,
            )
            return {
                "index": 0,
                "command_id": "inspect",
                "runtime_limit_ms": 10_000,
                "done": True,
                "outcome": {
                    "result": outcome.result.to_mapping(),
                    "diagnostic": None,
                },
            }
        return original_request(
            self,
            method,
            params,
            timeout_ms=timeout_ms,
            capability_fd=capability_fd,
        )

    monkeypatch.setattr(_WorkerProcess, "request", recording_request)
    with _candidate_rig(tmp_path, suffix="artifact-proxy") as rig:
        try:
            candidate = worker.bind_candidate(
                store=rig.store,
                lease=rig.lease,
                base_head=rig.head,
                revision_id=rig.revision_id,
            )
            session = worker.create_empty(candidate)
            program = _inspect_program(base_revision=rig.head.revision_id)
            (
                _snapshot,
                _directory,
                descriptor,
                snapshot_capability,
                proxy_envelope,
            ) = _snapshot_directory(
                tmp_path,
                task_id=program.task_id,
                project_id=rig.head.project_id,
                base_revision=rig.head.revision_id,
            )

            outcomes = worker.execute_program(
                program=program,
                candidate=candidate,
                session=session,
                artifact_snapshot=proxy_envelope,
                artifact_snapshot_fd=None if snapshot_capability is not None else descriptor,
                artifact_snapshot_capability=(
                    snapshot_capability.to_mapping()
                    if snapshot_capability is not None
                    else None
                ),
            )

            assert len(outcomes) == 1
            assert outcomes[0].result.ok is True
            assert len(calls) == 1
            assert calls[0][0]["artifact_snapshot"] == proxy_envelope
            if snapshot_capability is None:
                assert "artifact_path_capability" not in calls[0][0]
                assert calls[0][1] == descriptor
            else:
                assert calls[0][0]["artifact_path_capability"] == (
                    snapshot_capability.to_mapping()
                )
                assert calls[0][1] is None
            _assert_snapshot_authority_live(descriptor, snapshot_capability)
            worker.close_session(session)
            worker.release_candidate(candidate)
        finally:
            _close_snapshot_authority(descriptor)
            worker.close()
