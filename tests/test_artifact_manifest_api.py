"""Public artifact-manifest API and lazy AgentApplication integration tests."""

from __future__ import annotations

import hashlib
import json
import stat
from pathlib import Path

import pytest

from vibecad.application.agent import AgentApplication
from vibecad.application.artifact_manifest import (
    ArtifactManifestError,
    ArtifactManifestErrorCode,
    ArtifactManifestService,
)
from vibecad.application.artifacts import (
    MAX_ARTIFACT_SOURCE_BYTES,
    ArtifactApi,
    ArtifactManifestRequest,
    ArtifactServiceErrorCode,
    ArtifactServicePortFailure,
)

TASK_ID = "task_0123456789abcdef0123456789abcdef"
PROJECT_ID = "project_0123456789abcdef0123456789abcdef"
REVISION_ID = "revision_0123456789abcdef0123456789abcdef"
DRAFT_ID = "draft_0123456789abcdef0123456789abcdef"
VERIFICATION_ID = "verification_0123456789abcdef0123456789abcdef"
MATERIALIZATION_ID = "materialization_" + "7" * 64
MANIFEST_SHA256 = "1" * 64
VERIFICATION_SHA256 = "2" * 64
OBSERVATION_SHA256 = "3" * 64
DELIVERY_SHA256 = "4" * 64
MODEL_ID = "artifact_11111111111111111111111111111111"
STEP_ID = "artifact_22222222222222222222222222222222"


def _request(*, draft: bool = False) -> dict[str, object]:
    return {
        "schema_version": 1,
        "task_id": TASK_ID,
        "expected_generation": 7,
        "revision_id": REVISION_ID,
        "draft_id": DRAFT_ID if draft else None,
    }


def _resource_uri(artifact_id: str) -> str:
    return f"vibecad://artifact/{MATERIALIZATION_ID}/{artifact_id}"


def _result(*, draft: bool = False, materialized: bool = False) -> dict[str, object]:
    return {
        "source_kind": "draft" if draft else "committed",
        "task_id": TASK_ID,
        "task_generation": 7,
        "project_id": PROJECT_ID,
        "revision_id": REVISION_ID,
        "draft_id": DRAFT_ID if draft else None,
        "manifest_sha256": MANIFEST_SHA256,
        "verification_id": VERIFICATION_ID,
        "acceptance_id": "artifact-manifest",
        "verification_digest": VERIFICATION_SHA256,
        "observation_digest": OBSERVATION_SHA256,
        "materialized": materialized,
        "materialization_id": MATERIALIZATION_ID if materialized else None,
        "delivery_manifest_sha256": DELIVERY_SHA256 if materialized else None,
        "artifacts": [
            {
                "schema_version": 1,
                "id": MODEL_ID,
                "name": "model.FCStd",
                "format": "fcstd",
                "sha256": "5" * 64,
                "size_bytes": 11,
                "resource_uri": _resource_uri(MODEL_ID) if materialized else None,
            },
            {
                "schema_version": 1,
                "id": STEP_ID,
                "name": "model.step",
                "format": "step",
                "sha256": "6" * 64,
                "size_bytes": 13,
                "resource_uri": _resource_uri(STEP_ID) if materialized else None,
            },
        ],
    }


class _Port:
    def __init__(
        self,
        value: object,
        *,
        error: Exception | None = None,
    ) -> None:
        self.value = value
        self.error = error
        self.calls: list[ArtifactManifestRequest] = []

    def get_artifact_manifest(self, *, request: ArtifactManifestRequest) -> object:
        self.calls.append(request)
        if self.error is not None:
            raise self.error
        return self.value


_INGRESS_ERRORS = (
    (
        {**_request(), "output_path": "/private/model.FCStd"},
        "unknown_field",
        "/output_path",
        "The request contains an unknown field.",
    ),
    (
        {key: value for key, value in _request().items() if key != "draft_id"},
        "missing_field",
        "/draft_id",
        "A required request field is missing.",
    ),
    (
        {**_request(), "schema_version": True},
        "invalid_type",
        "/schema_version",
        "A request value has an invalid type.",
    ),
    (
        {**_request(), "schema_version": 2},
        "unsupported_version",
        "/schema_version",
        "The request schema version is not supported.",
    ),
    (
        {**_request(), "task_id": 1},
        "invalid_type",
        "/task_id",
        "A request value has an invalid type.",
    ),
    (
        {**_request(), "task_id": "task_BAD"},
        "invalid_value",
        "/task_id",
        "A request value is invalid.",
    ),
    (
        {**_request(), "expected_generation": True},
        "invalid_type",
        "/expected_generation",
        "A request value has an invalid type.",
    ),
    (
        {**_request(), "expected_generation": -1},
        "invalid_value",
        "/expected_generation",
        "A request value is invalid.",
    ),
    (
        {**_request(), "revision_id": "revision_BAD"},
        "invalid_value",
        "/revision_id",
        "A request value is invalid.",
    ),
    (
        {**_request(), "draft_id": 1},
        "invalid_type",
        "/draft_id",
        "A request value has an invalid type.",
    ),
    (
        {**_request(), "draft_id": "draft_BAD"},
        "invalid_value",
        "/draft_id",
        "A request value is invalid.",
    ),
)


@pytest.mark.parametrize(("submitted", "code", "path", "message"), _INGRESS_ERRORS)
def test_manifest_ingress_requires_exact_five_fields_before_port_dispatch(
    submitted: object,
    code: str,
    path: str,
    message: str,
) -> None:
    port = _Port(_result())

    response = ArtifactApi(port=port).get_artifact_manifest(submitted)

    assert response == {
        "schema_version": 1,
        "ok": False,
        "result": None,
        "error": {
            "schema_version": 1,
            "code": code,
            "path": path,
            "message": message,
        },
    }
    assert port.calls == []


@pytest.mark.parametrize("submitted", (None, [], "manifest", 1, True))
def test_manifest_ingress_rejects_non_mapping_values_without_port_dispatch(
    submitted: object,
) -> None:
    port = _Port(_result())

    response = ArtifactApi(port=port).get_artifact_manifest(submitted)

    assert response["error"] == {
        "schema_version": 1,
        "code": "invalid_type",
        "path": "",
        "message": "A request value has an invalid type.",
    }
    assert port.calls == []


@pytest.mark.parametrize("draft", (False, True))
@pytest.mark.parametrize("materialized", (False, True))
def test_manifest_success_projects_committed_draft_and_delivery_states_exactly(
    draft: bool,
    materialized: bool,
) -> None:
    value = _result(draft=draft, materialized=materialized)
    port = _Port(value)

    response = ArtifactApi(port=port).get_artifact_manifest(_request(draft=draft))

    assert response == {
        "schema_version": 1,
        "ok": True,
        "result": {"schema_version": 1, **value},
        "error": None,
    }
    assert port.calls == [
        ArtifactManifestRequest(
            task_id=TASK_ID,
            expected_generation=7,
            revision_id=REVISION_ID,
            draft_id=DRAFT_ID if draft else None,
        )
    ]


@pytest.mark.parametrize(
    "code",
    (
        ArtifactServiceErrorCode.NOT_FOUND,
        ArtifactServiceErrorCode.CONFLICT,
        ArtifactServiceErrorCode.INTEGRITY_FAILURE,
        ArtifactServiceErrorCode.RECOVERY_REQUIRED,
    ),
)
def test_manifest_neutral_port_failures_keep_fixed_public_errors(
    code: ArtifactServiceErrorCode,
) -> None:
    port = _Port(ArtifactServicePortFailure(code=code))

    response = ArtifactApi(port=port).get_artifact_manifest(_request())

    assert response["ok"] is False
    assert response["result"] is None
    assert response["error"]["code"] == code.value
    assert response["error"]["path"] == ""
    assert json.dumps(response).find(TASK_ID) == -1


def test_manifest_port_exception_is_fixed_internal_error_and_never_reflected() -> None:
    port = _Port(None, error=RuntimeError("/private/model.FCStd"))

    response = ArtifactApi(port=port).get_artifact_manifest(_request())

    assert response["error"] == {
        "schema_version": 1,
        "code": "internal_error",
        "path": "",
        "message": "The request could not be completed.",
    }
    assert "private" not in json.dumps(response)


def _malformed_result(case: str) -> object:
    value = _result(materialized=True)
    if case == "not_mapping":
        return []
    if case == "extra_field":
        value["path"] = "/private/model.FCStd"
    elif case == "wrong_task_binding":
        value["task_id"] = "task_ffffffffffffffffffffffffffffffff"
    elif case == "source_kind_mismatch":
        value["source_kind"] = "draft"
    elif case == "materialization_without_flag":
        value["materialized"] = False
    elif case == "missing_delivery_digest":
        value["delivery_manifest_sha256"] = None
    elif case == "wrong_resource_uri":
        value["artifacts"][0]["resource_uri"] = _resource_uri(STEP_ID)
    elif case == "duplicate_artifacts":
        value["artifacts"][1]["id"] = MODEL_ID
        value["artifacts"][1]["resource_uri"] = _resource_uri(MODEL_ID)
    elif case == "swapped_artifact_names":
        value["artifacts"][0]["name"] = "model.step"
    elif case == "oversized_artifact":
        value["artifacts"][0]["size_bytes"] = MAX_ARTIFACT_SOURCE_BYTES + 1
    else:  # pragma: no cover - closed local test fixture
        raise AssertionError(case)
    return value


@pytest.mark.parametrize(
    "case",
    (
        "not_mapping",
        "extra_field",
        "wrong_task_binding",
        "source_kind_mismatch",
        "materialization_without_flag",
        "missing_delivery_digest",
        "wrong_resource_uri",
        "duplicate_artifacts",
        "swapped_artifact_names",
        "oversized_artifact",
    ),
)
def test_malformed_manifest_port_results_fail_closed_as_internal_error(case: str) -> None:
    response = ArtifactApi(port=_Port(_malformed_result(case))).get_artifact_manifest(_request())

    assert response["error"] == {
        "schema_version": 1,
        "code": "internal_error",
        "path": "",
        "message": "The request could not be completed.",
    }
    assert "private" not in json.dumps(response)


def _tree(root: Path) -> tuple[tuple[str, int, int, int, str], ...]:
    values = []
    for path in sorted((root, *root.rglob("*"))):
        metadata = path.lstat()
        relative = "." if path == root else path.relative_to(root).as_posix()
        digest = ""
        if stat.S_ISREG(metadata.st_mode):
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
        values.append(
            (
                relative,
                metadata.st_mode,
                metadata.st_size,
                metadata.st_mtime_ns,
                digest,
            )
        )
    return tuple(values)


def _data_root(tmp_path: Path) -> Path:
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    return home / "data"


def test_virgin_agent_manifest_failure_is_read_only_and_keeps_heavy_components_lazy(
    tmp_path: Path,
) -> None:
    app = AgentApplication.open(data_root=_data_root(tmp_path))
    artifacts = app._layout.artifacts  # noqa: SLF001
    before = _tree(artifacts)

    response = app.get_artifact_manifest_request(
        {
            "schema_version": 1,
            "task_id": TASK_ID,
            "expected_generation": 0,
            "revision_id": REVISION_ID,
            "draft_id": None,
        }
    )

    assert response["error"] == {
        "schema_version": 1,
        "code": "not_found",
        "path": "",
        "message": "The task or revision was not found.",
    }
    assert _tree(artifacts) == before
    assert app._artifact_api is not None  # noqa: SLF001
    assert app._artifact_store is None  # noqa: SLF001
    assert app._artifact_service is None  # noqa: SLF001
    assert app._artifact_authority is None  # noqa: SLF001
    assert app._cad_validation_port is None  # noqa: SLF001
    assert app._runtimes == {}  # noqa: SLF001
    app.close()


def test_agent_maps_every_core_manifest_failure_without_initializing_export_path(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    app = AgentApplication.open(data_root=_data_root(tmp_path))
    artifacts = app._layout.artifacts  # noqa: SLF001
    before = _tree(artifacts)

    for code in ArtifactManifestErrorCode:

        def fail(
            self,
            *,
            task_id: object,
            expected_generation: object,
            revision_id: object,
            draft_id: object,
            selected: ArtifactManifestErrorCode = code,
        ) -> dict[str, object]:
            del self, task_id, expected_generation, revision_id, draft_id
            raise ArtifactManifestError(selected)

        monkeypatch.setattr(ArtifactManifestService, "get_artifact_manifest", fail)
        response = app.get_artifact_manifest_request(
            {
                "schema_version": 1,
                "task_id": TASK_ID,
                "expected_generation": 0,
                "revision_id": REVISION_ID,
                "draft_id": None,
            }
        )
        assert response["error"]["code"] == code.value
        assert response["error"]["path"] == ""

    assert _tree(artifacts) == before
    assert app._artifact_store is None  # noqa: SLF001
    assert app._artifact_service is None  # noqa: SLF001
    assert app._artifact_authority is None  # noqa: SLF001
    assert app._cad_validation_port is None  # noqa: SLF001
    assert app._runtimes == {}  # noqa: SLF001
    app.close()
