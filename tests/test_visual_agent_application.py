from __future__ import annotations

import os
import threading
from pathlib import Path
from types import SimpleNamespace

from PIL import Image

from tests.test_visual_review_artifacts import _artifact
from vibecad.application.agent import AgentApplication
from vibecad.visual.contracts import CalibrationStatus, ImageMime, ViewRole
from vibecad.visual.fake_provider import DeterministicFakeVisualProvider
from vibecad.visual.inputs import (
    DescriptorSource,
    ImageIngress,
    SealImageSetRequest,
    bind_visual_input_locator,
)


def _data_root(tmp_path: Path) -> Path:
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    return home / "data"


def _seal(application: AgentApplication, tmp_path: Path):
    source = tmp_path / "front.png"
    Image.new("RGB", (16, 12), (20, 80, 140)).save(source, format="PNG")
    os.chmod(source, 0o600)
    request = SealImageSetRequest(
        create_key="image_set_create_" + "1" * 32,
        inputs=(
            ImageIngress(
                view_role=ViewRole.FRONT,
                calibration_status=CalibrationStatus.UNKNOWN,
                declared_mime=ImageMime.PNG,
            ),
        ),
        unit="mm",
        dimension_hints=(),
        calibration_evidence=(),
        same_object=True,
        same_state=True,
        same_scale=True,
    )
    descriptor = os.open(source, os.O_RDONLY | os.O_CLOEXEC)
    try:
        return application.seal_visual_image_set(
            request=request,
            sources=(
                DescriptorSource(
                    fd=descriptor,
                    locator=bind_visual_input_locator(request, 0, os.fstat(descriptor)),
                ),
            ),
        )
    finally:
        os.close(descriptor)


def test_descriptor_ingress_seals_without_starting_a_visual_provider(tmp_path: Path) -> None:
    provider_calls = 0

    def provider_factory():
        nonlocal provider_calls
        provider_calls += 1
        raise AssertionError("descriptor ingress must not start a provider")

    application = AgentApplication.open(
        data_root=_data_root(tmp_path),
        visual_provider_factory=provider_factory,
    )
    try:
        sealed = _seal(application, tmp_path)
    finally:
        application.close()

    assert sealed.id.startswith("image_set_")
    assert sealed.manifest_sha256
    assert provider_calls == 0


def test_create_captures_current_head_replays_after_head_change_and_survives_restart(
    tmp_path: Path,
    monkeypatch,
) -> None:
    data_root = _data_root(tmp_path)
    provider_calls = 0

    def provider_factory():
        nonlocal provider_calls
        provider_calls += 1
        return DeterministicFakeVisualProvider({})

    application = AgentApplication.open(
        data_root=data_root,
        visual_provider_factory=provider_factory,
    )
    bootstrap = application.bootstrap_empty()
    sealed = _seal(application, tmp_path)
    request = {
        "schema_version": 1,
        "create_key": "reconstruction_create_" + "2" * 32,
        "project_id": bootstrap.head.project_id,
        "image_set_id": sealed.id,
        "image_set_manifest_sha256": sealed.manifest_sha256,
    }
    created = application.create_reconstruction_request(request)
    assert created["ok"] is True
    result = created["result"]
    assert result == {
        "schema_version": 1,
        "reconstruction_id": result["reconstruction_id"],
        "status": "ready",
        "generation": 0,
        "next_action": "run",
        "questions": [],
        "proposal_summary": None,
        "review_resources": [],
    }
    draft = application._visual_service.get(result["reconstruction_id"])
    assert draft.base_head.project_id == bootstrap.head.project_id
    assert draft.base_head.generation == bootstrap.head.generation
    assert draft.base_head.revision_id == bootstrap.head.revision_id
    assert draft.base_head.manifest_sha256 == bootstrap.head.manifest_sha256

    rejected = application.reject_reconstruction_request(
        {
            "schema_version": 1,
            "reconstruction_id": result["reconstruction_id"],
            "expected_generation": 0,
        }
    )
    assert rejected["ok"] is True
    assert rejected["result"]["status"] == "rejected"

    def changed_head_must_not_be_recaptured(self, *, project_id: str):
        raise AssertionError(f"replay recaptured changed HEAD for {project_id}")

    monkeypatch.setattr(AgentApplication, "get_project", changed_head_must_not_be_recaptured)
    assert application.create_reconstruction_request(request) == rejected

    mismatched = application.create_reconstruction_request(
        {**request, "project_id": "project_" + "9" * 32}
    )
    assert mismatched["ok"] is False
    assert mismatched["error"]["code"] == "conflict"
    application.close()

    restarted = AgentApplication.open(
        data_root=data_root,
        visual_provider_factory=provider_factory,
    )
    try:
        observed = restarted.get_reconstruction_request(
            {
                "schema_version": 1,
                "reconstruction_id": result["reconstruction_id"],
            }
        )
    finally:
        restarted.close()

    assert observed == rejected
    assert provider_calls == 2


def test_create_reuses_strict_parser_before_visual_composition(tmp_path: Path) -> None:
    provider_calls = 0

    def provider_factory():
        nonlocal provider_calls
        provider_calls += 1
        raise AssertionError("invalid ingress must not compose a provider")

    application = AgentApplication.open(
        data_root=_data_root(tmp_path),
        visual_provider_factory=provider_factory,
    )
    request = {
        "schema_version": 1,
        "create_key": "reconstruction_create_" + "2" * 32,
        "project_id": "project_" + "3" * 32,
        "image_set_id": "image_set_" + "4" * 32,
        "image_set_manifest_sha256": "5" * 64,
        "extension/~": True,
    }
    try:
        rejected = application.create_reconstruction_request(request)
    finally:
        application.close()

    assert rejected["ok"] is False
    assert rejected["error"] == {
        "schema_version": 1,
        "code": "unknown_field",
        "path": "/extension~1~0",
        "message": "The request contains an unknown field.",
    }
    assert provider_calls == 0


def test_captured_application_publishes_and_replays_visual_review_png(tmp_path: Path) -> None:
    data_root = _data_root(tmp_path)
    artifact = _artifact()
    application = AgentApplication.open(data_root=data_root)
    try:
        assert application.publish_visual_review_artifact(artifact) == artifact
        first = application.read_visual_review_resource(artifact.resource_uri)
        assert first.data == artifact.overlay.png_bytes
        assert first.media_type == "image/png"

        class ReviewService:
            def get(self, reconstruction_id: str):
                assert reconstruction_id == artifact.reconstruction_id
                return object()

            def load_presentation(self, _draft):
                return (
                    SimpleNamespace(
                        id=artifact.observation_id,
                        digest=artifact.observation_digest,
                    ),
                    None,
                )

        attached = application._attach_visual_review_resources(  # noqa: SLF001
            {
                "schema_version": 1,
                "ok": True,
                "result": {"reconstruction_id": artifact.reconstruction_id},
                "error": None,
            },
            ReviewService(),
        )
        assert attached["result"]["review_resources"] == [
            {
                "source_index": artifact.source_index,
                "observation_id": artifact.observation_id,
                "observation_digest": artifact.observation_digest,
                "resource_uri": artifact.resource_uri,
                "media_type": "image/png",
                "sha256": artifact.overlay.png_sha256,
                "size_bytes": artifact.overlay.png_size_bytes,
            }
        ]
    finally:
        application.close()

    restarted = AgentApplication.open(data_root=data_root)
    try:
        replay = restarted.read_visual_review_resource(artifact.resource_uri)
    finally:
        restarted.close()

    assert replay.uri == artifact.resource_uri
    assert replay.data == artifact.overlay.png_bytes


def test_visual_mutations_are_sequential_across_daemon_threads(tmp_path: Path) -> None:
    application = AgentApplication.open(data_root=_data_root(tmp_path))
    adopt_entered = threading.Event()
    allow_adopt = threading.Event()
    run_entered = threading.Event()

    class ProbeApi:
        def adopt_reconstruction(self, request):
            adopt_entered.set()
            if not allow_adopt.wait(timeout=5):
                raise RuntimeError("test adopt timeout")
            return {"action": "adopt", "request": request}

        def run_reconstruction(self, request):
            run_entered.set()
            return {"action": "run", "request": request}

    application._visual_api = ProbeApi()
    application._visual_service = object()
    outcomes: dict[str, object] = {}

    adopt_thread = threading.Thread(
        target=lambda: outcomes.setdefault(
            "adopt",
            application.adopt_reconstruction_request({"generation": 1}),
        )
    )
    run_thread = threading.Thread(
        target=lambda: outcomes.setdefault(
            "run",
            application.run_reconstruction_request({"generation": 1}),
        )
    )
    try:
        adopt_thread.start()
        assert adopt_entered.wait(timeout=5)
        run_thread.start()
        assert not run_entered.wait(timeout=0.1)
        allow_adopt.set()
        adopt_thread.join(timeout=5)
        run_thread.join(timeout=5)
    finally:
        allow_adopt.set()
        adopt_thread.join(timeout=5)
        run_thread.join(timeout=5)
        application.close()

    assert not adopt_thread.is_alive()
    assert not run_thread.is_alive()
    assert outcomes == {
        "adopt": {"action": "adopt", "request": {"generation": 1}},
        "run": {"action": "run", "request": {"generation": 1}},
    }
