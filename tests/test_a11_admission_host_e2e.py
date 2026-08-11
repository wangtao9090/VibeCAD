"""Host-neutral raw-MCP and daemon-restart gate for A11 visual admission."""

from __future__ import annotations

import dataclasses
import json
import shutil
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

import pytest
from PIL import Image

from tests.test_application_proposal_admission import (
    _invocation_record,
    _observation_for,
    _proposal_for,
    _succeeded,
)
from tests.test_application_proposal_evidence_evaluator import (
    _basis,
    _claim,
    _feature,
    _metric_landmarks,
    _proposal,
    _sealed_inputs,
)
from tests.test_mcp_transport import (
    _ChunkSource,
    _FrameSink,
    _initialize_owned,
    _run_owned,
)
from tests.test_p0b_acceptance import _ReviewCadPort
from tests.test_visual_preflight import _save, _seal
from vibecad import mcp_transport, server
from vibecad.application.agent import AgentApplication
from vibecad.application.proposal_admission import admit_proposal_evidence
from vibecad.daemon import LocalAgentClient, LocalKernelDaemon, LocalKernelState
from vibecad.visual.drafts import BaseHeadBinding, ReconstructionDraft, reconstruction_payload
from vibecad.visual.geometry_fit import PrimitiveFamily
from vibecad.visual.provider_images import prepare_provider_image_batch
from vibecad.visual.reconstruction import (
    ReconstructionStatus,
    reconstruction_identity,
)

pytestmark = pytest.mark.skipif(
    sys.platform != "darwin",
    reason="the authenticated local daemon is currently a macOS capability",
)

_CREATE_KEY = "reconstruction_create_" + "a" * 32


@pytest.fixture
def short_case_root() -> Path:
    root = Path(tempfile.mkdtemp(prefix="vc-a11-", dir="/private/tmp"))
    root.chmod(0o700)
    try:
        yield root
    finally:
        shutil.rmtree(root)
        assert not root.exists()


@dataclass(frozen=True, slots=True)
class _PreparedCase:
    data_root: Path
    project_id: str
    reconstruction_id: str
    generation: int
    sidecar: Path


class _ApplicationSlot:
    def __init__(self, client: LocalAgentClient) -> None:
        self.client = client

    def get(self) -> LocalAgentClient:
        return self.client


class _RawMcpSession:
    def __init__(self) -> None:
        self.source = _ChunkSource()
        self.sink = _FrameSink()
        self.runner, self.thread, _lifecycle, _exits = _run_owned(
            mcp_transport,
            server._owned_dispatch_descriptor,  # noqa: SLF001
            self.source,
            self.sink,
            failure_response=server._owned_failure_response,  # noqa: SLF001
        )
        try:
            _initialize_owned(self.source, self.sink, self.runner)
        except BaseException as primary:
            try:
                self.close()
            except BaseException as cleanup:
                primary.add_note(f"raw MCP initialization cleanup failed: {type(cleanup).__name__}")
            raise
        self._request_id = 0

    def rpc(self, method: str, params: dict[str, object] | None = None) -> dict[str, object]:
        self._request_id += 1
        expected = len(self.sink.messages) + 1
        self.source.send(
            {
                "jsonrpc": "2.0",
                "id": self._request_id,
                "method": method,
                "params": {} if params is None else params,
            }
        )
        response = self.sink.wait_for(expected)[-1]
        assert response["id"] == self._request_id
        return response

    def tool(self, name: str, arguments: dict[str, object]) -> dict[str, object]:
        response = self.rpc(
            "tools/call",
            {"name": name, "arguments": arguments},
        )
        assert "error" not in response
        result = response["result"]
        assert type(result) is dict
        envelope = result["structuredContent"]
        assert type(envelope) is dict
        return envelope

    def close(self) -> None:
        failure = None
        try:
            self.source.close()
        except BaseException as error:
            failure = error
        try:
            self.thread.join(10)
        except BaseException as error:
            if failure is None:
                failure = error
            else:
                failure.add_note(f"raw MCP runner join failed: {type(error).__name__}")
        if self.thread.is_alive():
            alive = AssertionError("raw MCP runner did not stop")
            if failure is None:
                failure = alive
            else:
                failure.add_note(str(alive))
        if failure is not None:
            raise failure


def _prepare_case(tmp_path: Path, *, mutation: str | None = None) -> _PreparedCase:
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    data_root = home / "data"
    application = AgentApplication.open(
        data_root=data_root,
        cad_port_factory=lambda **_kwargs: _ReviewCadPort(),
    )
    try:
        bootstrap = application.bootstrap_empty()
        inputs = application._visual_inputs_for_ingress()  # noqa: SLF001
        application._visual_bundle_for_request()  # noqa: SLF001
        drafts = application._visual_drafts  # noqa: SLF001
        assert drafts is not None

        source = tmp_path / "source.png"
        _save(source, blank=False)
        with Image.open(source) as image:
            image.resize((1001, 751), Image.Resampling.NEAREST).save(source, format="PNG")
        image_set = _seal(inputs, (source,))
        sealed, normalized = inputs.read_provider_images_exact(
            image_set.id,
            image_set.manifest_sha256,
        )
        batch = prepare_provider_image_batch(
            image_set=sealed,
            normalized_images=normalized,
            profile=_sealed_inputs()[1].profile,
            detail_crops=(),
        )
        template = _proposal(
            image_set_id=sealed.id,
            manifest=sealed.manifest_sha256,
        )
        features = (
            _feature(
                provider_image_id=batch.parts[0].id,
                local_id="plate-profile",
                family=PrimitiveFamily.ROTATED_RECTANGLE,
                claims=tuple(
                    _claim(template, name)
                    for name in ("profile", "edge0", "edge1", "edge2", "edge3")
                ),
                points=(
                    (0.05, 0.05),
                    (0.95, 0.05),
                    (0.95, 0.95),
                    (0.05, 0.95),
                ),
            ),
        )
        landmarks = _metric_landmarks()
        basis = dataclasses.replace(_basis(), frame_id="front-plane")

        reconstruction_id, create_digest = reconstruction_identity(_CREATE_KEY)
        head = bootstrap.head
        base_head = BaseHeadBinding(
            project_id=head.project_id,
            generation=head.generation,
            revision_id=head.revision_id,
            manifest_sha256=head.manifest_sha256,
        )
        ready = ReconstructionDraft(
            reconstruction_id=reconstruction_id,
            create_key_sha256=create_digest,
            generation=0,
            status=ReconstructionStatus.READY,
            base_head=base_head,
            image_set_id=sealed.id,
            image_set_manifest_sha256=sealed.manifest_sha256,
        )
        drafts.create(ready)

        first_intent = _invocation_record(
            reconstruction_id=reconstruction_id,
            generation=1,
            image_set_id=sealed.id,
            manifest=sealed.manifest_sha256,
            answer_digests=(),
        )
        observing = dataclasses.replace(
            ready,
            generation=1,
            status=ReconstructionStatus.OBSERVING,
            provider_invocations=(first_intent,),
        )
        drafts.compare_and_set(reconstruction_id, 0, observing)
        first_observation = _observation_for(
            template.observation,
            reconstruction_id=reconstruction_id,
            generation=1,
        )
        first_observation_payload = reconstruction_payload(first_observation)
        first_receipt = _succeeded(first_intent, "1")
        needs_input = dataclasses.replace(
            observing,
            generation=2,
            status=ReconstructionStatus.NEEDS_INPUT,
            observation_ref=first_observation_payload.ref,
            provider_invocations=(first_receipt,),
        )
        drafts.compare_and_set(
            reconstruction_id,
            1,
            needs_input,
            (first_observation_payload,),
        )

        answer_payloads = tuple(
            reconstruction_payload(item) for item in template.clarification_answers
        )
        ready_again = dataclasses.replace(
            needs_input,
            generation=3,
            status=ReconstructionStatus.READY,
            clarification_refs=tuple(item.ref for item in answer_payloads),
        )
        drafts.compare_and_set(
            reconstruction_id,
            2,
            ready_again,
            answer_payloads,
        )
        answer_digests = tuple(item.ref.contract_digest for item in answer_payloads)
        second_intent = _invocation_record(
            reconstruction_id=reconstruction_id,
            generation=4,
            image_set_id=sealed.id,
            manifest=sealed.manifest_sha256,
            answer_digests=answer_digests,
        )
        observing_again = dataclasses.replace(
            ready_again,
            generation=4,
            status=ReconstructionStatus.OBSERVING,
            provider_invocations=(first_receipt, second_intent),
        )
        drafts.compare_and_set(reconstruction_id, 3, observing_again)

        final_observation = _observation_for(
            template.observation,
            reconstruction_id=reconstruction_id,
            generation=4,
        )
        proposal = _proposal_for(template, final_observation)
        observation_payload = reconstruction_payload(final_observation)
        proposal_payload = reconstruction_payload(proposal)
        proposed = dataclasses.replace(
            observing_again,
            generation=5,
            status=ReconstructionStatus.PROPOSED,
            observation_ref=observation_payload.ref,
            proposal_ref=proposal_payload.ref,
            provider_invocations=(first_receipt, _succeeded(second_intent, "4")),
        )
        drafts.compare_and_set(
            reconstruction_id,
            4,
            proposed,
            (observation_payload, proposal_payload),
        )
        admit_proposal_evidence(
            reconstruction_store=drafts,
            visual_input_store=inputs,
            proposal=proposal,
            expected_generation=proposed.generation,
            image_batch=batch,
            provider_features=features,
            calibration_landmarks=landmarks,
            metric_basis=basis,
        )
        sidecar = next(
            (application._layout.reconstruction_drafts / reconstruction_id).glob(  # noqa: SLF001
                "admission_inputs_*.json"
            )
        )
        prepared = _PreparedCase(
            data_root=data_root,
            project_id=head.project_id,
            reconstruction_id=reconstruction_id,
            generation=proposed.generation,
            sidecar=sidecar,
        )
    finally:
        application.close()

    if mutation == "missing":
        prepared.sidecar.unlink()
    elif mutation == "tampered":
        raw = bytearray(prepared.sidecar.read_bytes())
        raw[-2] ^= 1
        prepared.sidecar.write_bytes(raw)
    elif mutation is not None:
        raise ValueError("unknown test mutation")
    return prepared


def _start_daemon(data_root: Path) -> tuple[LocalKernelDaemon, LocalAgentClient]:
    def application_factory(*, layout, lease_manager):
        return AgentApplication.from_captured_layout(
            layout=layout,
            lease_manager=lease_manager,
            cad_port_factory=lambda **_kwargs: _ReviewCadPort(),
        )

    daemon = LocalKernelDaemon.start(
        data_root=data_root,
        application_factory=application_factory,
    )
    try:
        client = LocalAgentClient.connect(
            daemon.run_root,
            artifact_root=data_root / "artifacts",
            release_root=data_root / "releases",
            visual_review_root=data_root / "visual_reviews",
        )
    except BaseException as primary:
        try:
            daemon.close()
        except BaseException as cleanup:
            primary.add_note(f"daemon connect cleanup failed: {type(cleanup).__name__}")
        raise
    return daemon, client


def _close_daemon(daemon: LocalKernelDaemon, client: LocalAgentClient) -> None:
    failure = None
    try:
        client.close()
    except BaseException as error:
        failure = error
    try:
        daemon.close()
    except BaseException as error:
        if failure is None:
            failure = error
        else:
            failure.add_note(f"daemon cleanup failed: {type(error).__name__}")
    if failure is not None:
        raise failure


def _close_session_and_daemon(
    session: _RawMcpSession | None,
    daemon: LocalKernelDaemon,
    client: LocalAgentClient,
) -> None:
    failure = None
    if session is not None:
        try:
            session.close()
        except BaseException as error:
            failure = error
    try:
        _close_daemon(daemon, client)
    except BaseException as error:
        if failure is None:
            failure = error
        else:
            failure.add_note(f"daemon teardown failed: {type(error).__name__}")
    if failure is not None:
        raise failure


def _bind_raw_server(monkeypatch: pytest.MonkeyPatch, client: LocalAgentClient) -> None:
    monkeypatch.setattr(server, "_application_slot", _ApplicationSlot(client))
    monkeypatch.setattr(server, "_application_runtime_guard", lambda: None)
    monkeypatch.setattr(server, "_enter_application_effect", lambda: True)


def _state(session: _RawMcpSession, project_id: str) -> dict[str, object]:
    return {
        "project": session.tool(
            "get_project",
            {"schema_version": 1, "project_id": project_id},
        ),
        "revisions": session.tool(
            "list_revisions",
            {"schema_version": 1, "project_id": project_id},
        ),
        "tasks": session.tool("list_tasks", {"schema_version": 1}),
    }


def _adopt(session: _RawMcpSession, case: _PreparedCase) -> dict[str, object]:
    return session.tool(
        "adopt_reconstruction",
        {
            "schema_version": 1,
            "reconstruction_id": case.reconstruction_id,
            "expected_generation": case.generation,
        },
    )


def test_daemon_connect_failure_closes_started_daemon(
    short_case_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started: list[LocalKernelDaemon] = []
    real_start = LocalKernelDaemon.start

    def capture_start(_cls, **kwargs):
        daemon = real_start(**kwargs)
        started.append(daemon)
        return daemon

    def fail_connect(_cls, *_args, **_kwargs):
        raise RuntimeError("injected connect failure")

    monkeypatch.setattr(LocalKernelDaemon, "start", classmethod(capture_start))
    monkeypatch.setattr(LocalAgentClient, "connect", classmethod(fail_connect))

    with pytest.raises(RuntimeError, match="injected connect failure"):
        _start_daemon(short_case_root / "data")

    assert len(started) == 1
    assert started[0].state is LocalKernelState.CLOSED


def test_raw_mcp_initialize_failure_closes_source_and_joins_runner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = sys.modules[__name__]
    sources = []
    threads = []
    original_source = _ChunkSource
    original_run = _run_owned

    class ObservedSource(original_source):
        closed = False

        def __init__(self) -> None:
            super().__init__()
            sources.append(self)

        def close(self) -> None:
            self.closed = True
            super().close()

    def capture_run(*args, **kwargs):
        result = original_run(*args, **kwargs)
        threads.append(result[1])
        return result

    def fail_initialize(*_args, **_kwargs):
        raise RuntimeError("injected initialize failure")

    monkeypatch.setattr(module, "_ChunkSource", ObservedSource)
    monkeypatch.setattr(module, "_run_owned", capture_run)
    monkeypatch.setattr(module, "_initialize_owned", fail_initialize)

    with pytest.raises(RuntimeError, match="injected initialize failure"):
        _RawMcpSession()

    assert len(sources) == len(threads) == 1
    assert sources[0].closed is True
    assert not threads[0].is_alive()


def test_teardown_attempts_session_client_and_daemon_cleanup_independently() -> None:
    calls = []

    class BrokenSession:
        def close(self) -> None:
            calls.append("session")
            raise RuntimeError("injected session close failure")

    class BrokenClient:
        def close(self) -> None:
            calls.append("client")
            raise RuntimeError("injected client close failure")

    class ObservedDaemon:
        def close(self) -> None:
            calls.append("daemon")

    with pytest.raises(RuntimeError, match="injected session close failure"):
        _close_session_and_daemon(BrokenSession(), ObservedDaemon(), BrokenClient())

    assert calls == ["session", "client", "daemon"]


def test_raw_stdio_discovery_matches_mcpb_and_keeps_adopt_schema_closed() -> None:
    session = _RawMcpSession()
    try:
        response = session.rpc("tools/list")
    finally:
        session.close()

    tools = response["result"]["tools"]
    manifest = json.loads((Path(__file__).parents[1] / "manifest.json").read_bytes())
    assert len(tools) == 38
    assert [item["name"] for item in tools] == [item["name"] for item in manifest["tools"]]
    adopt = next(item for item in tools if item["name"] == "adopt_reconstruction")
    assert adopt["inputSchema"] == {
        "type": "object",
        "properties": {
            "schema_version": {"const": 1, "type": "integer"},
            "reconstruction_id": {
                "pattern": "^reconstruction_[0-9a-f]{32}$",
                "type": "string",
            },
            "expected_generation": {
                "minimum": 0,
                "maximum": 9_007_199_254_740_991,
                "type": "integer",
            },
        },
        "required": ["schema_version", "reconstruction_id", "expected_generation"],
        "additionalProperties": False,
    }


def test_valid_sidecar_adopts_over_raw_stdio_and_daemon_restart_does_not_duplicate_task(
    short_case_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = _prepare_case(short_case_root)
    daemon, client = _start_daemon(case.data_root)
    session = None
    try:
        _bind_raw_server(monkeypatch, client)
        session = _RawMcpSession()
        before = _state(session, case.project_id)
        adopted = _adopt(session, case)
        after = _state(session, case.project_id)
    finally:
        _close_session_and_daemon(session, daemon, client)

    assert adopted["ok"] is True
    assert adopted["result"]["status"] == "adopted"
    assert before["project"] == after["project"]
    assert before["revisions"] == after["revisions"]
    assert before["tasks"]["result"]["tasks"] == []
    assert len(after["tasks"]["result"]["tasks"]) == 1

    restarted_daemon, restarted_client = _start_daemon(case.data_root)
    restarted_session = None
    try:
        _bind_raw_server(monkeypatch, restarted_client)
        restarted_session = _RawMcpSession()
        recovered = restarted_session.tool(
            "get_reconstruction",
            {"schema_version": 1, "reconstruction_id": case.reconstruction_id},
        )
        replay = _adopt(restarted_session, case)
        restarted_state = _state(restarted_session, case.project_id)
    finally:
        _close_session_and_daemon(
            restarted_session,
            restarted_daemon,
            restarted_client,
        )

    assert recovered["ok"] is True
    assert recovered["result"]["status"] == "adopted"
    assert replay["ok"] is False
    assert replay["error"]["code"] == "conflict"
    assert restarted_state == after


@pytest.mark.parametrize("mutation", ["missing", "tampered"])
def test_missing_or_tampered_sidecar_fails_closed_without_task_revision_or_head_change(
    short_case_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
) -> None:
    case = _prepare_case(short_case_root, mutation=mutation)
    daemon, client = _start_daemon(case.data_root)
    session = None
    try:
        _bind_raw_server(monkeypatch, client)
        session = _RawMcpSession()
        before = _state(session, case.project_id)
        rejected = _adopt(session, case)
        after = _state(session, case.project_id)
    finally:
        _close_session_and_daemon(session, daemon, client)

    assert rejected["ok"] is False
    assert rejected["error"]["code"] == "integrity_failure"
    assert before == after
    assert after["tasks"]["result"]["tasks"] == []
