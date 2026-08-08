from __future__ import annotations

from pathlib import Path

import pytest

from tests.test_visual_adoption import _AdoptionProbe
from tests.test_visual_service import (
    _budget,
    _head,
    _invocation,
    _observation,
    _PendingProbeProvider,
    _proposal,
    _question_observation,
    _sealed_image_set,
    _stores,
)
from vibecad.application.visual_api import (
    VisualApi,
    VisualApiErrorCode,
    VisualCreateIngressRequest,
)
from vibecad.runtime.contracts import RuntimeDiagnostic
from vibecad.visual.fake_provider import (
    DeterministicFakeVisualProvider,
    FakeVisualFixture,
    FakeVisualOutcomeKind,
)
from vibecad.visual.provider import VisualProviderBinding, visual_provider_input_digest
from vibecad.visual.review_store import VisualReviewStoreError, VisualReviewStoreErrorCode
from vibecad.visual.service import VisualReconstructionService

_CREATE_KEY = "reconstruction_create_" + "1" * 32
_MISSING_ID = "reconstruction_" + "0" * 32
_DEADLINE_MS = 2_000_000_000_000


def _service(inputs, drafts, provider, adoption=None) -> VisualReconstructionService:
    return VisualReconstructionService(
        inputs=inputs,
        drafts=drafts,
        provider=VisualProviderBinding(provider=provider),
        adoption=adoption,
    )


def _base_head_mapping() -> dict[str, object]:
    head = _head()
    return {
        "schema_version": 1,
        "project_id": head.project_id,
        "generation": head.generation,
        "revision_id": head.revision_id,
        "manifest_sha256": head.manifest_sha256,
    }


def _create_request(image_set) -> dict[str, object]:
    return {
        "schema_version": 1,
        "create_key": _CREATE_KEY,
        "image_set_id": image_set.id,
        "image_set_manifest_sha256": image_set.manifest_sha256,
        "base_head": _base_head_mapping(),
    }


def _run_request(
    reconstruction_id: str,
    generation: int,
    *,
    invoke: bool = True,
) -> dict[str, object]:
    budget = _budget()
    return {
        "schema_version": 1,
        "reconstruction_id": reconstruction_id,
        "expected_generation": generation,
        "budget": (
            {
                "max_elapsed_ms": budget.max_elapsed_ms,
                "max_memory_bytes": budget.max_memory_bytes,
                "max_output_bytes": budget.max_output_bytes,
            }
            if invoke
            else None
        ),
        "deadline_ms": _DEADLINE_MS if invoke else None,
    }


def _mutation_request(reconstruction_id: str, generation: int) -> dict[str, object]:
    return {
        "schema_version": 1,
        "reconstruction_id": reconstruction_id,
        "expected_generation": generation,
    }


def _result(response: dict[str, object]) -> dict[str, object]:
    assert set(response) == {"schema_version", "ok", "result", "error"}
    assert response["schema_version"] == 1
    assert response["ok"] is True
    assert response["error"] is None
    result = response["result"]
    assert type(result) is dict
    return result


def _error(response: dict[str, object], code: str, path: str = "") -> None:
    assert response == {
        "schema_version": 1,
        "ok": False,
        "result": None,
        "error": {
            "schema_version": 1,
            "code": code,
            "path": path,
            "message": response["error"]["message"],
        },
    }
    assert type(response["error"]["message"]) is str
    assert response["error"]["message"]


def test_create_get_reject_and_delete_return_only_host_safe_fields(tmp_path: Path) -> None:
    inputs, drafts = _stores(tmp_path)
    image_set = _sealed_image_set(tmp_path, inputs)
    api = VisualApi(service=_service(inputs, drafts, DeterministicFakeVisualProvider({})))

    created = _result(api.create_reconstruction(_create_request(image_set)))
    assert created == {
        "schema_version": 1,
        "reconstruction_id": created["reconstruction_id"],
        "status": "ready",
        "generation": 0,
        "next_action": "run",
        "questions": [],
        "proposal_summary": None,
    }
    reconstruction_id = created["reconstruction_id"]
    assert (
        _result(
            api.get_reconstruction({"schema_version": 1, "reconstruction_id": reconstruction_id})
        )
        == created
    )

    rejected = _result(api.reject_reconstruction(_mutation_request(reconstruction_id, 0)))
    assert rejected["status"] == "rejected"
    assert rejected["generation"] == 1
    assert rejected["next_action"] == "none"

    deleted = _result(api.delete_reconstruction(_mutation_request(reconstruction_id, 1)))
    assert deleted["status"] == "deleted"
    assert deleted["next_action"] == "none"
    assert deleted["questions"] == []
    assert deleted["proposal_summary"] is None
    rendered = repr(deleted)
    for forbidden in (
        "image_set",
        "manifest",
        "sha256",
        "payload",
        "path",
        "provider",
    ):
        assert forbidden not in rendered


def test_review_cleanup_store_failure_maps_to_bounded_public_error() -> None:
    def fail():
        raise VisualReviewStoreError(VisualReviewStoreErrorCode.DURABILITY_UNCERTAIN)

    _error(VisualApi._guard(fail), "recovery_required")  # noqa: SLF001


def test_run_and_answer_project_only_bounded_actionable_question(tmp_path: Path) -> None:
    inputs, drafts = _stores(tmp_path)
    image_set = _sealed_image_set(tmp_path, inputs)
    invocation = _invocation(image_set)
    observation = _question_observation(invocation)
    provider = DeterministicFakeVisualProvider(
        {
            visual_provider_input_digest(invocation): FakeVisualFixture(
                kind=FakeVisualOutcomeKind.OBSERVATION,
                value=observation,
            )
        }
    )
    api = VisualApi(service=_service(inputs, drafts, provider))
    created = _result(api.create_reconstruction(_create_request(image_set)))

    needs_input = _result(api.run_reconstruction(_run_request(created["reconstruction_id"], 0)))
    question = observation.questions[0]
    assert needs_input["status"] == "needs_input"
    assert needs_input["next_action"] == "answer"
    assert needs_input["questions"] == [
        {
            "question_id": question.id,
            "kind": "confirm_assumption",
            "prompt": "Confirm the assumed depth",
        }
    ]
    assert needs_input["proposal_summary"] is None

    answered = _result(
        api.answer_reconstruction(
            {
                "schema_version": 1,
                "reconstruction_id": created["reconstruction_id"],
                "expected_generation": needs_input["generation"],
                "question_id": question.id,
                "response": True,
            }
        )
    )
    assert answered["status"] == "ready"
    assert answered["next_action"] == "run"
    assert answered["questions"] == []

    stale = api.answer_reconstruction(
        {
            "schema_version": 1,
            "reconstruction_id": created["reconstruction_id"],
            "expected_generation": needs_input["generation"],
            "question_id": question.id,
            "response": True,
        }
    )
    _error(stale, "conflict")


def test_proposal_and_adoption_expose_only_minimum_review_information(tmp_path: Path) -> None:
    inputs, drafts = _stores(tmp_path)
    image_set = _sealed_image_set(tmp_path, inputs)
    invocation = _invocation(image_set)
    proposal = _proposal(_observation(invocation))
    provider = DeterministicFakeVisualProvider(
        {
            visual_provider_input_digest(invocation): FakeVisualFixture(
                kind=FakeVisualOutcomeKind.PROPOSAL,
                value=proposal,
            )
        }
    )
    adoption = _AdoptionProbe(drafts=drafts)
    api = VisualApi(service=_service(inputs, drafts, provider, adoption))
    created = _result(api.create_reconstruction(_create_request(image_set)))
    proposed = _result(api.run_reconstruction(_run_request(created["reconstruction_id"], 0)))

    assert proposed["status"] == "proposed"
    assert proposed["next_action"] == "adopt_or_reject"
    assert proposed["proposal_summary"] == {
        "part_type": "mounting_plate",
        "summary": "One editable circular plate reconstructed from visual evidence.",
    }
    rendered = repr(proposed)
    for forbidden in (
        "alternatives",
        "unsupported",
        "diagnostic",
        "image_set",
        "payload",
        "provider",
    ):
        assert forbidden not in rendered

    adopted = _result(
        api.adopt_reconstruction(
            _mutation_request(created["reconstruction_id"], proposed["generation"])
        )
    )
    assert adopted["status"] == "adopted"
    assert adopted["next_action"] == "review_task"
    assert adopted["adopted_task_id"].startswith("task_")
    assert adopted["proposal_summary"] == proposed["proposal_summary"]
    assert len(adoption.ensure_calls) == 1


def test_strict_requests_reject_coercion_extensions_and_runtime_shape(tmp_path: Path) -> None:
    inputs, drafts = _stores(tmp_path)
    image_set = _sealed_image_set(tmp_path, inputs)
    api = VisualApi(service=_service(inputs, drafts, DeterministicFakeVisualProvider({})))

    create = _create_request(image_set)
    invalid_version = dict(create, schema_version=True)
    _error(api.create_reconstruction(invalid_version), "invalid_type", "/schema_version")

    extended = dict(create, image_base64="secret-image")
    _error(api.create_reconstruction(extended), "unknown_field", "/image_base64")

    missing = dict(create)
    del missing["base_head"]
    _error(api.create_reconstruction(missing), "missing_field", "/base_head")

    bad_head = dict(create)
    bad_head["base_head"] = dict(_base_head_mapping(), generation="4")
    _error(
        api.create_reconstruction(bad_head),
        "invalid_type",
        "/base_head/generation",
    )

    created = _result(api.create_reconstruction(create))
    reconstruction_id = created["reconstruction_id"]
    bad_run = _run_request(reconstruction_id, 0)
    bad_run["budget"] = {
        "max_elapsed_ms": "1000",
        "max_memory_bytes": 1,
        "max_output_bytes": 1,
    }
    _error(
        api.run_reconstruction(bad_run),
        "invalid_type",
        "/budget/max_elapsed_ms",
    )

    half_reconcile = _run_request(reconstruction_id, 0, invoke=False)
    half_reconcile["deadline_ms"] = _DEADLINE_MS
    _error(api.run_reconstruction(half_reconcile), "invalid_value", "/budget")

    bad_generation = _mutation_request(reconstruction_id, True)
    _error(
        api.reject_reconstruction(bad_generation),
        "invalid_type",
        "/expected_generation",
    )

    cyclic: dict[str, object] = {"schema_version": 1}
    cyclic["reconstruction_id"] = cyclic
    _error(api.get_reconstruction(cyclic), "invalid_value", "/reconstruction_id")


def test_failures_are_stable_and_provider_diagnostics_never_escape(tmp_path: Path) -> None:
    inputs, drafts = _stores(tmp_path)
    image_set = _sealed_image_set(tmp_path, inputs)
    invocation = _invocation(image_set)
    provider = DeterministicFakeVisualProvider(
        {
            visual_provider_input_digest(invocation): FakeVisualFixture(
                kind=FakeVisualOutcomeKind.FAILURE,
                diagnostic=RuntimeDiagnostic(
                    code="fixture.private",
                    message="secret /private/provider/path",
                    retryable=False,
                ),
            )
        }
    )
    api = VisualApi(service=_service(inputs, drafts, provider))

    missing = api.get_reconstruction({"schema_version": 1, "reconstruction_id": _MISSING_ID})
    _error(missing, "not_found")
    assert "reconstruction_" not in repr(missing)

    created = _result(api.create_reconstruction(_create_request(image_set)))
    failed = _result(api.run_reconstruction(_run_request(created["reconstruction_id"], 0)))
    assert failed["status"] == "failed"
    assert failed["next_action"] == "run"
    assert "secret" not in repr(failed)
    assert "private" not in repr(failed)
    assert "diagnostic" not in repr(failed)


def test_run_uses_null_runtime_inputs_to_reconcile_without_restarting(tmp_path: Path) -> None:
    inputs, drafts = _stores(tmp_path)
    image_set = _sealed_image_set(tmp_path, inputs)
    provider = _PendingProbeProvider(lambda _invocation: None)
    service = _service(inputs, drafts, provider)
    api = VisualApi(service=service)
    created = _result(api.create_reconstruction(_create_request(image_set)))

    observing = _result(api.run_reconstruction(_run_request(created["reconstruction_id"], 0)))
    assert observing["status"] == "observing"
    assert observing["next_action"] == "wait"
    assert provider.starts == 1

    restarted_api = VisualApi(service=_service(inputs, drafts, provider))
    reconciled = _result(
        restarted_api.run_reconstruction(
            _run_request(
                created["reconstruction_id"],
                observing["generation"],
                invoke=False,
            )
        )
    )
    assert reconciled["status"] == "observing"
    assert reconciled["next_action"] == "wait"
    assert provider.starts == 1
    assert provider.reconciles == 1


def test_composition_failure_helper_has_fixed_messages_and_bounded_paths() -> None:
    failure = VisualApi.failure(VisualApiErrorCode.NOT_FOUND, "/project_id")
    _error(failure, "not_found", "/project_id")

    with pytest.raises(TypeError):
        VisualApi.failure("not_found")
    with pytest.raises(ValueError):
        VisualApi.failure(VisualApiErrorCode.NOT_FOUND, "project_id")
    with pytest.raises(ValueError):
        VisualApi.failure(VisualApiErrorCode.NOT_FOUND, "/" + "x" * 300)


def test_public_create_parser_reuses_strict_budgets_and_pointer_escaping() -> None:
    request = {
        "schema_version": 1,
        "create_key": _CREATE_KEY,
        "project_id": _head().project_id,
        "image_set_id": "image_set_" + "2" * 32,
        "image_set_manifest_sha256": "3" * 64,
    }
    parsed = VisualApi.parse_create_request(request)
    assert parsed == VisualCreateIngressRequest(
        create_key=request["create_key"],
        project_id=request["project_id"],
        image_set_id=request["image_set_id"],
        image_set_manifest_sha256=request["image_set_manifest_sha256"],
    )

    special_key = dict(request)
    special_key["extension/~"] = True
    rejected = VisualApi.parse_create_request(special_key)
    assert type(rejected) is dict
    _error(rejected, "unknown_field", "/extension~1~0")

    oversized_key = dict(request)
    oversized_key["x" * 129] = True
    rejected = VisualApi.parse_create_request(oversized_key)
    assert type(rejected) is dict
    _error(rejected, "budget_exceeded")

    oversized_request = dict(request)
    oversized_request["extension"] = "x" * (16 * 1024 + 1)
    rejected = VisualApi.parse_create_request(oversized_request)
    assert type(rejected) is dict
    _error(rejected, "budget_exceeded", "/extension")

    too_many_nodes = dict(request)
    too_many_nodes["extension"] = list(range(257))
    rejected = VisualApi.parse_create_request(too_many_nodes)
    assert type(rejected) is dict
    _error(rejected, "budget_exceeded", "/extension/249")

    cyclic = dict(request)
    cyclic["project_id"] = cyclic
    rejected = VisualApi.parse_create_request(cyclic)
    assert type(rejected) is dict
    _error(rejected, "invalid_value", "/project_id")
