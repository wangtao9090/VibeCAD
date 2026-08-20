from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path

from vibecad import workbuddy_adapter

TASK_ID = "task_11111111111111111111111111111111"
BASE_REVISION = "revision_22222222222222222222222222222222"
REQUEST_NAME = ".vibecad-workbuddy-request-test.json"


def _program(*, operation: dict[str, object] | None = None) -> dict[str, object]:
    if operation is None:
        operation = {
            "schema_version": 1,
            "id": "create-box",
            "op": "create_box",
            "target": {},
            "args": {"length_mm": 80, "width_mm": 50, "height_mm": 8},
            "preserve": [],
            "source": "model",
            "depends_on": [],
        }
    return {
        "schema_version": 1,
        "task_id": TASK_ID,
        "base_revision": BASE_REVISION,
        "operations": [operation],
        "acceptance": {
            "schema_version": 1,
            "id": "workbuddy-adapter-test",
            "criteria": [],
        },
    }


def _request(*, program: dict[str, object] | None = None) -> dict[str, object]:
    return {
        "schema_version": 1,
        "task_id": TASK_ID,
        "expected_generation": 0,
        "program": _program() if program is None else program,
    }


def _write_request(root: Path, value: object, *, mode: int = 0o600) -> bytes:
    raw = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()
    path = root / REQUEST_NAME
    path.write_bytes(raw)
    path.chmod(mode)
    return raw


class _Client:
    def __init__(self, envelope: dict[str, object]) -> None:
        self.envelope = envelope
        self.requests: list[dict[str, object]] = []
        self.closed = False

    def submit_model_program_request(self, request: object) -> dict[str, object]:
        assert type(request) is dict
        self.requests.append(request)
        return self.envelope

    def close(self) -> None:
        self.closed = True


def _success_envelope() -> dict[str, object]:
    return {
        "schema_version": 1,
        "ok": True,
        "result": {
            "generation": 7,
            "next_action": "review_draft",
            "task_run": {
                "id": TASK_ID,
                "project_id": "project_33333333333333333333333333333333",
                "status": "awaiting_user_review",
                "base_revision": BASE_REVISION,
                "candidate_revision": "revision_44444444444444444444444444444444",
                "committed_revision": None,
                "draft": {"id": "draft_55555555555555555555555555555555"},
                "last_error": None,
                "verification_reports": [{"passed": True}],
            },
        },
        "error": None,
    }


def test_submit_file_compacts_program_and_returns_bounded_task_summary(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    raw = _write_request(tmp_path, _request())
    client = _Client(_success_envelope())

    result = workbuddy_adapter.submit_request_file(
        REQUEST_NAME,
        client_factory=lambda: client,
    )

    assert result == {
        "schema_version": 1,
        "ok": True,
        "result": {
            "task_id": TASK_ID,
            "project_id": "project_33333333333333333333333333333333",
            "generation": 7,
            "status": "awaiting_user_review",
            "next_action": "review_draft",
            "base_revision": BASE_REVISION,
            "candidate_revision": "revision_44444444444444444444444444444444",
            "committed_revision": None,
            "draft_id": "draft_55555555555555555555555555555555",
            "last_error": None,
            "verification_passed": True,
        },
        "error": None,
        "request_sha256": hashlib.sha256(raw).hexdigest(),
    }
    assert client.closed is True
    assert client.requests == [
        {
            "schema_version": 1,
            "task_id": TASK_ID,
            "expected_generation": 0,
            "program_json": json.dumps(
                _program(),
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
            ),
        }
    ]


def test_submit_file_preserves_domain_error_without_marking_cli_failure(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    _write_request(tmp_path, _request())
    domain_error = {
        "schema_version": 1,
        "ok": False,
        "result": None,
        "error": {
            "schema_version": 1,
            "code": "conflict",
            "path": "/expected_generation",
            "message": "The task record changed concurrently.",
        },
    }
    client = _Client(domain_error)

    result = workbuddy_adapter.submit_request_file(
        REQUEST_NAME,
        client_factory=lambda: client,
    )

    assert result["ok"] is False
    assert result["error"] == domain_error["error"]
    assert len(result["request_sha256"]) == 64
    assert client.closed is True


def test_parametric_preflight_returns_exact_nested_contract_path_without_submission(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    operation = {
        "schema_version": 1,
        "id": "create-parametric-design",
        "op": "create_parametric_design",
        "target": {},
        "args": {"design": {}},
        "preserve": [],
        "source": "model",
        "depends_on": [],
    }
    _write_request(tmp_path, _request(program=_program(operation=operation)))

    result = workbuddy_adapter.submit_request_file(
        REQUEST_NAME,
        client_factory=lambda: (_ for _ in ()).throw(AssertionError("must not submit")),
    )

    assert result["ok"] is False
    assert result["error"] == {
        "schema_version": 1,
        "code": "missing_field",
        "path": "/program/operations/0/args/design/body",
        "message": "A required field is missing.",
    }


def test_request_ingress_rejects_duplicate_fields_and_unsafe_file(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    duplicate = (
        b'{"schema_version":1,"schema_version":1,"task_id":"'
        + TASK_ID.encode()
        + b'","expected_generation":0,"program":{}}'
    )
    path = tmp_path / REQUEST_NAME
    path.write_bytes(duplicate)
    path.chmod(0o600)

    duplicated = workbuddy_adapter.submit_request_file(REQUEST_NAME)
    assert duplicated["error"]["code"] == "invalid_request_file"

    path.write_text(json.dumps(_request()), encoding="utf-8")
    if sys.platform == "win32":
        os.link(path, tmp_path / "second-link.json")
    else:
        path.chmod(0o622)
    unsafe = workbuddy_adapter.submit_request_file(REQUEST_NAME)
    assert unsafe["error"]["code"] == "unsafe_request_file"


def test_request_ingress_rejects_paths_and_symlinks(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    target = tmp_path / "target.json"
    target.write_text(json.dumps(_request()), encoding="utf-8")
    target.chmod(0o600)
    os.symlink(target.name, tmp_path / REQUEST_NAME)

    linked = workbuddy_adapter.submit_request_file(REQUEST_NAME)
    escaped = workbuddy_adapter.submit_request_file("../request.json")

    assert linked["error"]["code"] == "unsafe_request_file"
    assert escaped["error"]["code"] == "invalid_request_file"
