"""Strict transport-neutral ProjectApi contract tests."""

from __future__ import annotations

import inspect
import json
import math
from dataclasses import FrozenInstanceError, replace

import pytest

import vibecad.application.project_api as project_api_module
from vibecad.application.project_api import (
    ProjectApi,
    ProjectApiErrorCode,
    ProjectCreateResult,
    ProjectCurrentResult,
    ProjectKind,
    ProjectServicePortErrorCode,
    ProjectServicePortFailure,
)
from vibecad.execution.revisions import ProjectHead, RevisionArtifactRef, RevisionRef

CREATE_KEY = "project_create_0123456789abcdef0123456789abcdef"
PROJECT_ID = "project_0123456789abcdef0123456789abcdef"
OTHER_PROJECT_ID = "project_11111111111111111111111111111111"
REVISION_ZERO = "revision_0123456789abcdef0123456789abcdef"
REVISION_ONE = "revision_11111111111111111111111111111111"
MODEL_ID = "artifact_0123456789abcdef0123456789abcdef"
STEP_ID = "artifact_11111111111111111111111111111111"
DIGEST_ZERO = "0" * 64
DIGEST_ONE = "1" * 64


class StringSubclass(str):
    pass


class DictSubclass(dict):
    pass


def _model() -> RevisionArtifactRef:
    return RevisionArtifactRef(
        id=MODEL_ID,
        name="model.FCStd",
        format="fcstd",
        sha256=DIGEST_ZERO,
        size_bytes=123,
    )


def _step() -> RevisionArtifactRef:
    return RevisionArtifactRef(
        id=STEP_ID,
        name="model.step",
        format="step",
        sha256=DIGEST_ONE,
        size_bytes=456,
    )


def _generation_zero(*, imported: bool = False) -> tuple[ProjectHead, RevisionRef]:
    head = ProjectHead(
        project_id=PROJECT_ID,
        generation=0,
        revision_id=REVISION_ZERO,
        manifest_sha256=DIGEST_ZERO,
    )
    revision = RevisionRef(
        id=REVISION_ZERO,
        project_id=PROJECT_ID,
        base_revision=None,
        manifest_sha256=DIGEST_ZERO,
        model=_model() if imported else None,
        artifacts=(),
    )
    return head, revision


def _current() -> tuple[ProjectHead, RevisionRef]:
    head = ProjectHead(
        project_id=PROJECT_ID,
        generation=1,
        revision_id=REVISION_ONE,
        manifest_sha256=DIGEST_ONE,
    )
    revision = RevisionRef(
        id=REVISION_ONE,
        project_id=PROJECT_ID,
        base_revision=REVISION_ZERO,
        manifest_sha256=DIGEST_ONE,
        model=_model(),
        artifacts=(_step(),),
    )
    return head, revision


def _create_result(
    *,
    kind: ProjectKind = ProjectKind.EMPTY,
    cleanup_required: bool = False,
) -> ProjectCreateResult:
    head, revision = _generation_zero(imported=kind is ProjectKind.IMPORT_FCSTD)
    return ProjectCreateResult(
        create_key=CREATE_KEY,
        kind=kind,
        cleanup_required=cleanup_required,
        project_id=PROJECT_ID,
        head=head,
        revision=revision,
    )


class RecordingPort:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.create_result: object = _create_result()
        head, revision = _current()
        self.current_result: object = ProjectCurrentResult(
            project_id=PROJECT_ID,
            head=head,
            revision=revision,
        )
        self.error: BaseException | None = None

    def create_project(self, **kwargs):
        self.calls.append(("create_project", kwargs))
        if self.error is not None:
            raise self.error
        return self.create_result

    def get_project(self, **kwargs):
        self.calls.append(("get_project", kwargs))
        if self.error is not None:
            raise self.error
        return self.current_result


def _error(response: dict[str, object], code: ProjectApiErrorCode, path: str = "") -> None:
    assert response == {
        "schema_version": 1,
        "ok": False,
        "result": None,
        "error": {
            "schema_version": 1,
            "code": code.value,
            "path": path,
            "message": project_api_module._ERROR_MESSAGES[code],
        },
    }


def _canonical_size(value: object) -> int:
    return len(
        json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    )


def test_public_api_taxonomies_and_exact_signatures_are_frozen() -> None:
    assert project_api_module.__all__ == (
        "ProjectApi",
        "ProjectApiErrorCode",
        "ProjectCreateResult",
        "ProjectCurrentResult",
        "ProjectKind",
        "ProjectServicePort",
        "ProjectServicePortErrorCode",
        "ProjectServicePortFailure",
    )
    assert {item.value for item in ProjectKind} == {"empty", "import_fcstd"}
    assert {item.value for item in ProjectApiErrorCode} == {
        "missing_field",
        "unknown_field",
        "unsupported_version",
        "invalid_type",
        "invalid_value",
        "budget_exceeded",
        "invalid_input",
        "not_found",
        "conflict",
        "lease_unavailable",
        "resource_exhausted",
        "runtime_unavailable",
        "integrity_failure",
        "cad_failure",
        "store_failure",
        "recovery_required",
        "internal_error",
    }
    assert {item.value for item in ProjectServicePortErrorCode} == {
        item.value
        for item in ProjectApiErrorCode
        if item
        not in {
            ProjectApiErrorCode.MISSING_FIELD,
            ProjectApiErrorCode.UNKNOWN_FIELD,
            ProjectApiErrorCode.UNSUPPORTED_VERSION,
            ProjectApiErrorCode.INVALID_TYPE,
            ProjectApiErrorCode.INVALID_VALUE,
            ProjectApiErrorCode.BUDGET_EXCEEDED,
        }
    }
    assert project_api_module._ERROR_MESSAGES[ProjectApiErrorCode.RUNTIME_UNAVAILABLE] == (
        "The managed CAD runtime is not active."
    )
    init = inspect.signature(ProjectApi.__init__).parameters
    assert tuple(init) == ("self", "port")
    assert init["port"].kind is inspect.Parameter.KEYWORD_ONLY
    for name in ("create_project", "get_project"):
        parameters = inspect.signature(getattr(ProjectApi, name)).parameters
        assert tuple(parameters) == ("self", "request")


def test_empty_create_has_exact_port_call_and_success_projection() -> None:
    port = RecordingPort()
    response = ProjectApi(port=port).create_project(
        {
            "schema_version": 1,
            "create_key": CREATE_KEY,
            "kind": "empty",
        }
    )
    assert port.calls == [
        (
            "create_project",
            {
                "create_key": CREATE_KEY,
                "kind": ProjectKind.EMPTY,
                "source_path": None,
            },
        )
    ]
    assert response == {
        "schema_version": 1,
        "ok": True,
        "result": {
            "schema_version": 1,
            "create_key": CREATE_KEY,
            "kind": "empty",
            "cleanup_required": False,
            "project_id": PROJECT_ID,
            "generation_zero": {
                "head": {
                    "schema_version": 1,
                    "project_id": PROJECT_ID,
                    "generation": 0,
                    "revision_id": REVISION_ZERO,
                    "manifest_sha256": DIGEST_ZERO,
                },
                "revision": {
                    "schema_version": 1,
                    "id": REVISION_ZERO,
                    "project_id": PROJECT_ID,
                    "base_revision": None,
                    "manifest_sha256": DIGEST_ZERO,
                    "model": None,
                    "artifacts": [],
                },
            },
        },
        "error": None,
    }


def test_import_create_projects_the_exact_normalized_model_shape() -> None:
    port = RecordingPort()
    port.create_result = _create_result(
        kind=ProjectKind.IMPORT_FCSTD,
        cleanup_required=True,
    )
    path = "/private/input.FCStd"
    response = ProjectApi(port=port).create_project(
        {
            "schema_version": 1,
            "create_key": CREATE_KEY,
            "kind": "import_fcstd",
            "source_path": path,
        }
    )
    assert port.calls == [
        (
            "create_project",
            {
                "create_key": CREATE_KEY,
                "kind": ProjectKind.IMPORT_FCSTD,
                "source_path": path,
            },
        )
    ]
    assert response["error"] is None
    result = response["result"]
    assert type(result) is dict
    assert result["cleanup_required"] is True
    model = result["generation_zero"]["revision"]["model"]
    assert model == {
        "schema_version": 1,
        "id": MODEL_ID,
        "name": "model.FCStd",
        "format": "fcstd",
        "sha256": DIGEST_ZERO,
        "size_bytes": 123,
    }
    assert result["generation_zero"]["revision"]["artifacts"] == []


def test_get_project_projects_current_head_revision_and_ordered_artifacts() -> None:
    port = RecordingPort()
    response = ProjectApi(port=port).get_project({"schema_version": 1, "project_id": PROJECT_ID})
    assert port.calls == [("get_project", {"project_id": PROJECT_ID})]
    assert response == {
        "schema_version": 1,
        "ok": True,
        "result": {
            "schema_version": 1,
            "project_id": PROJECT_ID,
            "current": {
                "head": {
                    "schema_version": 1,
                    "project_id": PROJECT_ID,
                    "generation": 1,
                    "revision_id": REVISION_ONE,
                    "manifest_sha256": DIGEST_ONE,
                },
                "revision": {
                    "schema_version": 1,
                    "id": REVISION_ONE,
                    "project_id": PROJECT_ID,
                    "base_revision": REVISION_ZERO,
                    "manifest_sha256": DIGEST_ONE,
                    "model": {
                        "schema_version": 1,
                        "id": MODEL_ID,
                        "name": "model.FCStd",
                        "format": "fcstd",
                        "sha256": DIGEST_ZERO,
                        "size_bytes": 123,
                    },
                    "artifacts": [
                        {
                            "schema_version": 1,
                            "id": STEP_ID,
                            "name": "model.step",
                            "format": "step",
                            "sha256": DIGEST_ONE,
                            "size_bytes": 456,
                        }
                    ],
                },
            },
        },
        "error": None,
    }


@pytest.mark.parametrize(
    ("payload", "code", "path"),
    [
        (None, ProjectApiErrorCode.INVALID_TYPE, ""),
        (DictSubclass(), ProjectApiErrorCode.INVALID_TYPE, ""),
        ({}, ProjectApiErrorCode.MISSING_FIELD, "/create_key"),
        (
            {"schema_version": 1, "create_key": CREATE_KEY, "kind": "empty", "extra": 1},
            ProjectApiErrorCode.UNKNOWN_FIELD,
            "/extra",
        ),
        (
            {"schema_version": 2, "create_key": CREATE_KEY, "kind": "empty"},
            ProjectApiErrorCode.UNSUPPORTED_VERSION,
            "/schema_version",
        ),
        (
            {"schema_version": True, "create_key": CREATE_KEY, "kind": "empty"},
            ProjectApiErrorCode.INVALID_TYPE,
            "/schema_version",
        ),
        (
            {"schema_version": 2**80, "create_key": CREATE_KEY, "kind": "empty"},
            ProjectApiErrorCode.INVALID_VALUE,
            "/schema_version",
        ),
        (
            {"schema_version": 1, "create_key": CREATE_KEY, "kind": True},
            ProjectApiErrorCode.INVALID_TYPE,
            "/kind",
        ),
        (
            {"schema_version": 1, "create_key": CREATE_KEY, "kind": "other"},
            ProjectApiErrorCode.INVALID_VALUE,
            "/kind",
        ),
        (
            {
                "schema_version": 1,
                "create_key": CREATE_KEY,
                "kind": "import_fcstd",
            },
            ProjectApiErrorCode.MISSING_FIELD,
            "/source_path",
        ),
        (
            {
                "schema_version": 1,
                "create_key": CREATE_KEY,
                "kind": "empty",
                "source_path": "/input.FCStd",
            },
            ProjectApiErrorCode.UNKNOWN_FIELD,
            "/source_path",
        ),
    ],
)
def test_create_schema_is_exact_noncoercing_and_conditional(payload, code, path) -> None:
    port = RecordingPort()
    response = ProjectApi(port=port).create_project(payload)
    _error(response, code, path)
    assert port.calls == []


@pytest.mark.parametrize(
    ("payload", "code", "path"),
    [
        (None, ProjectApiErrorCode.INVALID_TYPE, ""),
        (DictSubclass(), ProjectApiErrorCode.INVALID_TYPE, ""),
        ({}, ProjectApiErrorCode.MISSING_FIELD, "/project_id"),
        (
            {"schema_version": 1, "project_id": PROJECT_ID, "extra": None},
            ProjectApiErrorCode.UNKNOWN_FIELD,
            "/extra",
        ),
        (
            {"schema_version": 2, "project_id": PROJECT_ID},
            ProjectApiErrorCode.UNSUPPORTED_VERSION,
            "/schema_version",
        ),
        (
            {"schema_version": False, "project_id": PROJECT_ID},
            ProjectApiErrorCode.INVALID_TYPE,
            "/schema_version",
        ),
        (
            {"schema_version": 1, "project_id": math.nan},
            ProjectApiErrorCode.INVALID_VALUE,
            "/project_id",
        ),
    ],
)
def test_get_schema_is_exact_and_noncoercing(payload, code, path) -> None:
    port = RecordingPort()
    response = ProjectApi(port=port).get_project(payload)
    _error(response, code, path)
    assert port.calls == []


def test_ingress_rejects_cycles_non_string_keys_and_bounds_unknown_paths() -> None:
    port = RecordingPort()
    api = ProjectApi(port=port)

    cyclic: dict[str, object] = {
        "schema_version": 1,
        "project_id": PROJECT_ID,
    }
    cyclic["loop"] = cyclic
    _error(
        api.get_project(cyclic),
        ProjectApiErrorCode.INVALID_VALUE,
        "/loop",
    )
    _error(
        api.get_project({"schema_version": 1, "project_id": PROJECT_ID, 1: None}),
        ProjectApiErrorCode.INVALID_TYPE,
        "",
    )
    _error(
        api.get_project({"schema_version": 1, "project_id": PROJECT_ID, "a/b~": None}),
        ProjectApiErrorCode.UNKNOWN_FIELD,
        "/a~1b~0",
    )
    _error(
        api.get_project({"schema_version": 1, "project_id": PROJECT_ID, "a" * 257: None}),
        ProjectApiErrorCode.BUDGET_EXCEEDED,
        "",
    )
    assert port.calls == []


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("create_key", "project_create_bad"),
        ("create_key", StringSubclass(CREATE_KEY)),
        ("create_key", True),
        ("project_id", "project_bad"),
        ("project_id", StringSubclass(PROJECT_ID)),
        ("project_id", 1),
    ],
)
def test_identifiers_are_exact_lowercase_grammars(field: str, value: object) -> None:
    port = RecordingPort()
    api = ProjectApi(port=port)
    if field == "create_key":
        response = api.create_project({"schema_version": 1, "create_key": value, "kind": "empty"})
    else:
        response = api.get_project({"schema_version": 1, "project_id": value})
    expected = (
        ProjectApiErrorCode.INVALID_TYPE
        if type(value) is not str
        else ProjectApiErrorCode.INVALID_VALUE
    )
    _error(response, expected, f"/{field}")
    assert port.calls == []


@pytest.mark.parametrize(
    "path",
    [
        "",
        "relative.FCStd",
        "../input.FCStd",
        "/private/../input.FCStd",
        "/private/./input.FCStd",
        "/private//input.FCStd",
        "/private/input.FCStd/",
        "file:///private/input.FCStd",
        "/",
        "/private/\x00input.FCStd",
    ],
)
def test_import_path_must_be_a_canonical_absolute_lexical_path(path: str) -> None:
    port = RecordingPort()
    response = ProjectApi(port=port).create_project(
        {
            "schema_version": 1,
            "create_key": CREATE_KEY,
            "kind": "import_fcstd",
            "source_path": path,
        }
    )
    _error(response, ProjectApiErrorCode.INVALID_VALUE, "/source_path")
    assert port.calls == []


def test_path_budget_accepts_4096_utf8_bytes_and_rejects_4097_before_port() -> None:
    port = RecordingPort()
    port.create_result = _create_result(kind=ProjectKind.IMPORT_FCSTD)
    api = ProjectApi(port=port)
    exact = "/" + "a" * 4095
    assert len(exact.encode("utf-8")) == 4096
    accepted = api.create_project(
        {
            "schema_version": 1,
            "create_key": CREATE_KEY,
            "kind": "import_fcstd",
            "source_path": exact,
        }
    )
    assert accepted["ok"] is True
    assert len(port.calls) == 1

    over = exact + "a"
    rejected = api.create_project(
        {
            "schema_version": 1,
            "create_key": CREATE_KEY,
            "kind": "import_fcstd",
            "source_path": over,
        }
    )
    _error(rejected, ProjectApiErrorCode.BUDGET_EXCEEDED, "/source_path")
    assert len(port.calls) == 1


def test_path_budget_is_utf8_based_and_rejects_string_subclasses() -> None:
    port = RecordingPort()
    port.create_result = _create_result(kind=ProjectKind.IMPORT_FCSTD)
    api = ProjectApi(port=port)
    exact = "/" + "界" * 1365
    assert len(exact.encode("utf-8")) == 4096
    assert (
        api.create_project(
            {
                "schema_version": 1,
                "create_key": CREATE_KEY,
                "kind": "import_fcstd",
                "source_path": exact,
            }
        )["ok"]
        is True
    )
    _error(
        api.create_project(
            {
                "schema_version": 1,
                "create_key": CREATE_KEY,
                "kind": "import_fcstd",
                "source_path": exact + "界",
            }
        ),
        ProjectApiErrorCode.BUDGET_EXCEEDED,
        "/source_path",
    )
    _error(
        api.create_project(
            {
                "schema_version": 1,
                "create_key": CREATE_KEY,
                "kind": "import_fcstd",
                "source_path": StringSubclass("/private/input.FCStd"),
            }
        ),
        ProjectApiErrorCode.INVALID_TYPE,
        "/source_path",
    )
    assert len(port.calls) == 1


def test_request_budget_has_exact_8192_and_8193_byte_boundaries() -> None:
    port = RecordingPort()
    api = ProjectApi(port=port)
    base = {"schema_version": 1, "project_id": PROJECT_ID, "padding": ""}
    overhead = _canonical_size(base)
    exact = {**base, "padding": "x" * (8192 - overhead)}
    over = {**base, "padding": "x" * (8193 - overhead)}
    assert _canonical_size(exact) == 8192
    assert _canonical_size(over) == 8193

    _error(
        api.get_project(exact),
        ProjectApiErrorCode.UNKNOWN_FIELD,
        "/padding",
    )
    _error(
        api.get_project(over),
        ProjectApiErrorCode.BUDGET_EXCEEDED,
        "",
    )
    assert port.calls == []


@pytest.mark.parametrize("port_code", list(ProjectServicePortErrorCode))
def test_every_neutral_port_failure_maps_exactly_without_invoking_twice(port_code) -> None:
    port = RecordingPort()
    port.current_result = ProjectServicePortFailure(code=port_code)
    response = ProjectApi(port=port).get_project({"schema_version": 1, "project_id": PROJECT_ID})
    _error(response, ProjectApiErrorCode(port_code.value))
    assert port.calls == [("get_project", {"project_id": PROJECT_ID})]


def test_port_exception_is_path_free_internal_error_and_called_at_most_once() -> None:
    port = RecordingPort()
    secret = "/Users/private/SECRET-input.FCStd"
    port.error = RuntimeError(secret)
    response = ProjectApi(port=port).create_project(
        {
            "schema_version": 1,
            "create_key": CREATE_KEY,
            "kind": "empty",
        }
    )
    _error(response, ProjectApiErrorCode.INTERNAL_ERROR)
    assert port.calls == [
        (
            "create_project",
            {
                "create_key": CREATE_KEY,
                "kind": ProjectKind.EMPTY,
                "source_path": None,
            },
        )
    ]
    assert secret not in json.dumps(response)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda value: replace(value, create_key="project_create_" + "f" * 32),
        lambda value: replace(value, project_id=OTHER_PROJECT_ID),
        lambda value: replace(
            value,
            head=replace(value.head, generation=1),
        ),
        lambda value: replace(
            value,
            revision=replace(value.revision, manifest_sha256=DIGEST_ONE),
        ),
    ],
)
def test_create_result_cross_fields_are_independently_revalidated(mutate) -> None:
    port = RecordingPort()
    port.create_result = mutate(_create_result())
    response = ProjectApi(port=port).create_project(
        {"schema_version": 1, "create_key": CREATE_KEY, "kind": "empty"}
    )
    _error(response, ProjectApiErrorCode.INTERNAL_ERROR)
    assert len(port.calls) == 1


def test_create_kind_requires_the_corresponding_generation_zero_model() -> None:
    port = RecordingPort()
    imported_head, imported_revision = _generation_zero(imported=True)
    port.create_result = ProjectCreateResult(
        create_key=CREATE_KEY,
        kind=ProjectKind.EMPTY,
        cleanup_required=False,
        project_id=PROJECT_ID,
        head=imported_head,
        revision=imported_revision,
    )
    response = ProjectApi(port=port).create_project(
        {"schema_version": 1, "create_key": CREATE_KEY, "kind": "empty"}
    )
    _error(response, ProjectApiErrorCode.INTERNAL_ERROR)

    empty_head, empty_revision = _generation_zero(imported=False)
    port.create_result = ProjectCreateResult(
        create_key=CREATE_KEY,
        kind=ProjectKind.IMPORT_FCSTD,
        cleanup_required=False,
        project_id=PROJECT_ID,
        head=empty_head,
        revision=empty_revision,
    )
    response = ProjectApi(port=port).create_project(
        {
            "schema_version": 1,
            "create_key": CREATE_KEY,
            "kind": "import_fcstd",
            "source_path": "/private/input.FCStd",
        }
    )
    _error(response, ProjectApiErrorCode.INTERNAL_ERROR)
    assert len(port.calls) == 2


def test_empty_create_cannot_report_import_cleanup_state() -> None:
    port = RecordingPort()
    port.create_result = _create_result(cleanup_required=True)
    response = ProjectApi(port=port).create_project(
        {"schema_version": 1, "create_key": CREATE_KEY, "kind": "empty"}
    )
    _error(response, ProjectApiErrorCode.INTERNAL_ERROR)
    assert len(port.calls) == 1


def test_get_result_must_be_an_exact_coherent_snapshot() -> None:
    port = RecordingPort()
    head, revision = _current()
    port.current_result = ProjectCurrentResult(
        project_id=PROJECT_ID,
        head=head,
        revision=replace(revision, manifest_sha256=DIGEST_ZERO),
    )
    response = ProjectApi(port=port).get_project({"schema_version": 1, "project_id": PROJECT_ID})
    _error(response, ProjectApiErrorCode.INTERNAL_ERROR)
    assert len(port.calls) == 1


def test_untrusted_result_shapes_do_not_trigger_implicit_protocols() -> None:
    class Explosive:
        def __getattr__(self, _name):
            raise AssertionError("implicit protocol executed")

        def __iter__(self):
            raise AssertionError("implicit protocol executed")

    port = RecordingPort()
    port.create_result = Explosive()
    response = ProjectApi(port=port).create_project(
        {"schema_version": 1, "create_key": CREATE_KEY, "kind": "empty"}
    )
    _error(response, ProjectApiErrorCode.INTERNAL_ERROR)
    assert len(port.calls) == 1


def test_forged_create_result_does_not_invoke_create_key_equality_hook() -> None:
    class EqualityTrap:
        def __init__(self) -> None:
            self.calls = 0

        def __eq__(self, _other: object) -> bool:
            self.calls += 1
            return False

    trap = EqualityTrap()
    valid = _create_result()
    forged = object.__new__(ProjectCreateResult)
    object.__setattr__(forged, "create_key", trap)
    object.__setattr__(forged, "kind", valid.kind)
    object.__setattr__(forged, "cleanup_required", valid.cleanup_required)
    object.__setattr__(forged, "project_id", valid.project_id)
    object.__setattr__(forged, "head", valid.head)
    object.__setattr__(forged, "revision", valid.revision)
    port = RecordingPort()
    port.create_result = forged

    response = ProjectApi(port=port).create_project(
        {"schema_version": 1, "create_key": CREATE_KEY, "kind": "empty"}
    )

    _error(response, ProjectApiErrorCode.INTERNAL_ERROR)
    assert trap.calls == 0
    assert len(port.calls) == 1


def test_forged_get_result_does_not_invoke_project_id_equality_hook() -> None:
    class EqualityTrap:
        def __init__(self) -> None:
            self.calls = 0

        def __eq__(self, _other: object) -> bool:
            self.calls += 1
            return False

    trap = EqualityTrap()
    head, revision = _current()
    forged = object.__new__(ProjectCurrentResult)
    object.__setattr__(forged, "project_id", trap)
    object.__setattr__(forged, "head", head)
    object.__setattr__(forged, "revision", revision)
    port = RecordingPort()
    port.current_result = forged

    response = ProjectApi(port=port).get_project({"schema_version": 1, "project_id": PROJECT_ID})

    _error(response, ProjectApiErrorCode.INTERNAL_ERROR)
    assert trap.calls == 0
    assert len(port.calls) == 1


def test_tampered_exact_result_and_failure_values_fail_closed() -> None:
    port = RecordingPort()
    failure = ProjectServicePortFailure(code=ProjectServicePortErrorCode.NOT_FOUND)
    object.__setattr__(failure, "code", "not_found")
    port.current_result = failure
    _error(
        ProjectApi(port=port).get_project({"schema_version": 1, "project_id": PROJECT_ID}),
        ProjectApiErrorCode.INTERNAL_ERROR,
    )

    head, revision = _current()
    object.__setattr__(revision, "artifacts", (object(),))
    port.current_result = ProjectCurrentResult(
        project_id=PROJECT_ID,
        head=head,
        revision=revision,
    )
    _error(
        ProjectApi(port=port).get_project({"schema_version": 1, "project_id": PROJECT_ID}),
        ProjectApiErrorCode.INTERNAL_ERROR,
    )
    assert len(port.calls) == 2


def test_failure_values_and_result_values_are_frozen_exact_types() -> None:
    with pytest.raises(TypeError):
        ProjectServicePortFailure(code="not_found")
    with pytest.raises(TypeError):
        ProjectCreateResult(
            create_key=CREATE_KEY,
            kind="empty",
            cleanup_required=False,
            project_id=PROJECT_ID,
            head=_generation_zero()[0],
            revision=_generation_zero()[1],
        )
    with pytest.raises(TypeError):
        ProjectCurrentResult(
            project_id=PROJECT_ID,
            head=object(),
            revision=_generation_zero()[1],
        )
    value = _create_result()
    with pytest.raises(FrozenInstanceError):
        value.project_id = OTHER_PROJECT_ID
