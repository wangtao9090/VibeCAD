from __future__ import annotations

import hashlib
import queue
import tempfile
import threading
import time
from pathlib import Path
from types import ModuleType

_MAIN_THREAD_ID = threading.get_ident()


def _require_main_thread() -> None:
    if threading.get_ident() != _MAIN_THREAD_ID:
        raise RuntimeError("fake widget thread authority violation")


def _find_qt_children(
    owner: object,
    child_type: type[object],
    name: str | None = None,
) -> list[object]:
    matches: list[object] = []
    for child in getattr(owner, "_qt_children", ()):
        if isinstance(child, child_type) and (
            not name or getattr(child, "object_name", "") == name
        ):
            matches.append(child)
        matches.extend(_find_qt_children(child, child_type, name))
    return matches


class FakeWorkbench:
    pass


class FakeSelectionRecord:
    def __init__(self, selected_object: object, subelements: tuple[str, ...]) -> None:
        self.Object = selected_object
        self.SubElementNames = list(subelements)


class FakeSelection:
    def __init__(self) -> None:
        self.observers: list[object] = []
        self.records: list[FakeSelectionRecord] = []

    def addObserver(self, observer: object) -> None:
        _require_main_thread()
        if observer in self.observers:
            raise RuntimeError("synthetic duplicate selection observer")
        self.observers.append(observer)

    def removeObserver(self, observer: object) -> None:
        _require_main_thread()
        self.observers.remove(observer)

    def getSelectionEx(self) -> list[FakeSelectionRecord]:
        _require_main_thread()
        return list(self.records)

    def setSelection(
        self,
        selected_object: object,
        *,
        subelements: tuple[str, ...] = (),
    ) -> None:
        _require_main_thread()
        self.records = [FakeSelectionRecord(selected_object, subelements)]
        for observer in tuple(self.observers):
            observer.addSelection("ignored-document", "ignored-object", "", (0.0, 0.0, 0.0))

    def clearSelection(self) -> None:
        _require_main_thread()
        self.records = []
        for observer in tuple(self.observers):
            observer.clearSelection("ignored-document")


class FakeFreeCADGui(ModuleType):
    def __init__(
        self,
        *,
        fail_first_add: bool = False,
        fail_first_dock_add: bool = False,
    ) -> None:
        super().__init__("FreeCADGui")
        self.added_workbenches: list[object] = []
        self.add_attempts = 0
        self._fail_first_add = fail_first_add
        self.main_window = FakeMainWindow(fail_first_add=fail_first_dock_add)
        self.Selection = FakeSelection()

    def addWorkbench(self, workbench: object) -> None:
        _require_main_thread()
        self.add_attempts += 1
        if self._fail_first_add and self.add_attempts == 1:
            raise RuntimeError("synthetic addWorkbench failure")
        self.added_workbenches.append(workbench)

    def getMainWindow(self) -> FakeMainWindow:
        _require_main_thread()
        return self.main_window


def make_fake_freecad_gui(
    *,
    fail_first_add: bool = False,
    fail_first_dock_add: bool = False,
) -> FakeFreeCADGui:
    return FakeFreeCADGui(
        fail_first_add=fail_first_add,
        fail_first_dock_add=fail_first_dock_add,
    )


class FakeDocument:
    def __init__(
        self,
        name: str,
        local_path: str,
        events: list[str],
        freecad: FakeFreeCAD,
    ) -> None:
        self.Name = name
        self.FileName = local_path
        self.Modified = False
        self.Document = self
        self.Objects: list[object] = []
        self._events = events
        self._freecad = freecad
        self._pending_model_change = False

    def recompute(self) -> None:
        _require_main_thread()
        self._events.append("document.recompute")

    def save(self) -> None:
        _require_main_thread()
        self._events.append("document.save")
        if self.Modified or self._pending_model_change:
            path = Path(self.FileName)
            path.write_bytes(path.read_bytes() + b"user edit\n")
            path.chmod(0o644)
        self.Modified = False
        self._pending_model_change = False

    def simulate_recomputed_edit(self) -> None:
        _require_main_thread()
        self._pending_model_change = True
        self.Modified = False
        self._freecad._notify_changed_object(self, "Length")
        self._events.append("document.recompute")


class FakeFreeCAD(ModuleType):
    def __init__(self, *, events: list[str] | None = None) -> None:
        super().__init__("FreeCAD")
        self.events = [] if events is None else events
        self.documents: dict[str, FakeDocument] = {}
        self.document_observers: list[object] = []
        self.opened_paths: list[str] = []
        self.close_failures = 0
        self._next_document_index: int | None = None

    def openDocument(self, local_path: str) -> FakeDocument:
        _require_main_thread()
        self.events.append("document.open")
        self.opened_paths.append(local_path)
        if self._next_document_index is None:
            self._next_document_index = len(self.documents) + 1
        name = f"VibeCADPreview{self._next_document_index}"
        self._next_document_index += 1
        document = FakeDocument(name, local_path, self.events, self)
        self.documents[name] = document
        return document

    def addDocumentObserver(self, observer: object) -> None:
        _require_main_thread()
        if observer in self.document_observers:
            raise RuntimeError("synthetic duplicate document observer")
        self.document_observers.append(observer)

    def removeDocumentObserver(self, observer: object) -> None:
        _require_main_thread()
        self.document_observers.remove(observer)

    def _notify_changed_object(self, changed_object: object, property_name: str) -> None:
        for observer in tuple(self.document_observers):
            callback = getattr(observer, "slotChangedObject", None)
            if callable(callback):
                callback(changed_object, property_name)

    def getDocument(self, name: str) -> FakeDocument | None:
        _require_main_thread()
        return self.documents.get(name)

    def listDocuments(self) -> dict[str, FakeDocument]:
        _require_main_thread()
        return dict(self.documents)

    def closeDocument(self, name: str) -> None:
        _require_main_thread()
        self.events.append("document.close")
        if self.close_failures:
            self.close_failures -= 1
            raise RuntimeError("synthetic document close failure")
        self.documents.pop(name, None)


def make_fake_freecad(*, events: list[str] | None = None) -> FakeFreeCAD:
    return FakeFreeCAD(events=events)


class FakeMainWindow:
    def __init__(self, *, fail_first_add: bool = False) -> None:
        self._qt_children: list[object] = []
        self.docks: list[object] = []
        self.add_attempts = 0
        self._fail_first_add = fail_first_add

    def addDockWidget(self, _area: object, dock: object) -> None:
        _require_main_thread()
        self.add_attempts += 1
        if self._fail_first_add and self.add_attempts == 1:
            raise RuntimeError("synthetic addDockWidget failure")
        self.docks.append(dock)

    def removeDockWidget(self, dock: object) -> None:
        _require_main_thread()
        self.docks.remove(dock)

    def children(self) -> list[object]:
        _require_main_thread()
        return list(self._qt_children)

    def findChildren(
        self,
        child_type: type[object],
        name: str | None = None,
    ) -> list[object]:
        _require_main_thread()
        return _find_qt_children(self, child_type, name)


class FakeLocalAgentClient:
    def __init__(
        self,
        *,
        events: list[str] | None = None,
        monotonic_preview_authorities: bool = False,
        draft_project_by_task_id: dict[str, str] | None = None,
        materialized_checkout_root: Path | None = None,
        materialized_model_bytes: bytes = b"VibeCAD materialized model\n",
    ) -> None:
        self.daemon_id = "daemon_" + "a" * 32
        self.calls: list[tuple[str, dict[str, object], int]] = []
        self.events = [] if events is None else events
        self.monotonic_preview_authorities = monotonic_preview_authorities is True
        self.draft_project_by_task_id = (
            {} if draft_project_by_task_id is None else dict(draft_project_by_task_id)
        )
        self._materialized_checkout_tmp: tempfile.TemporaryDirectory[str] | None = None
        if materialized_checkout_root is None:
            self._materialized_checkout_tmp = tempfile.TemporaryDirectory(
                prefix="vibecad-fake-checkouts-"
            )
            self.materialized_checkout_root = Path(self._materialized_checkout_tmp.name).resolve()
        else:
            self.materialized_checkout_root = materialized_checkout_root.resolve()
        self.materialized_model_bytes = bytes(materialized_model_bytes)
        self.created_thread_id = threading.get_ident()
        self.closed_thread_id: int | None = None
        self.close_call_count = 0
        self.ping_failures = 0
        self.review_failure = False
        self.review_effect_after_loss = False
        self.review_entered: threading.Event | None = None
        self.review_release: threading.Event | None = None
        self.review_task_generation = 3
        self.review_task_status = "awaiting_user_review"
        self.review_committed_revision: str | None = None
        self.review_project_generation = 2
        self.review_project_revision = "revision_" + "1" * 32
        self.last_tasks_response: dict[str, object] | None = None
        self.projects_response: dict[str, object] | None = None
        self.tasks_response: dict[str, object] | None = None
        self.checkout_close_failures = 0
        self.checkout_close_responses: list[dict[str, object]] = []
        self.client_close_failures = 0
        self.claim_file_grant_failures = 0
        self.open_checkout_entered: threading.Event | None = None
        self.open_checkout_release: threading.Event | None = None
        self.open_checkout_transform: object | None = None
        self.checkout_descriptors: dict[str, dict[str, object]] = {}
        self.checkout_paths: dict[str, Path] = {}
        self.checkout_file_observations: dict[str, tuple[object, object, object]] = {}
        self._next_preview_authority = 6
        self._preview_grant_claims: dict[str, dict[str, object]] = {}

    def _record(self, name: str, request: dict[str, object]) -> None:
        if threading.get_ident() != self.created_thread_id:
            raise RuntimeError("fake client thread authority violation")
        self.calls.append((name, request, threading.get_ident()))

    def ping(self) -> dict[str, object]:
        self._record("ping", {})
        if self.ping_failures:
            self.ping_failures -= 1
            raise RuntimeError("synthetic ping failure")
        return {"schema_version": 1, "status": "ready"}

    def list_projects_request(self, request: dict[str, object]) -> dict[str, object]:
        self._record("list_projects", request)
        if self.projects_response is not None:
            return self.projects_response
        response = {
            "schema_version": 1,
            "ok": True,
            "result": {
                "schema_version": 1,
                "projects": [
                    {
                        "schema_version": 1,
                        "project_id": "project_" + "1" * 32,
                        "generation": 2,
                        "revision_id": "revision_" + "2" * 32,
                        "manifest_sha256": "3" * 64,
                    }
                ],
                "next_cursor": None,
            },
            "error": None,
        }
        return response

    def list_tasks_request(self, request: dict[str, object]) -> dict[str, object]:
        self._record("list_tasks", request)
        if self.tasks_response is not None:
            self.last_tasks_response = self.tasks_response
            return self.tasks_response
        response = {
            "schema_version": 1,
            "ok": True,
            "result": {
                "tasks": [
                    _fake_task("1", status="awaiting_user_review"),
                    _fake_task("2", status="active"),
                ],
                "next_cursor": None,
            },
            "error": None,
        }
        self.last_tasks_response = response
        return response

    def get_project_request(self, request: dict[str, object]) -> dict[str, object]:
        self._record("get_project", request)
        return _fake_project_detail(
            generation=self.review_project_generation,
            revision_id=self.review_project_revision,
        )

    def get_task_request(self, request: dict[str, object]) -> dict[str, object]:
        self._record("get_task", request)
        digit = "2" if request.get("task_id") == "task_" + "2" * 32 else "1"
        if digit == "2":
            return _fake_task_detail(digit, status="active")
        return _fake_task_detail(
            digit,
            generation=self.review_task_generation,
            status=self.review_task_status,
            committed_revision=self.review_committed_revision,
        )

    def accept_draft_request(self, request: dict[str, object]) -> dict[str, object]:
        self._record("accept_draft", request)
        if self.review_entered is not None:
            self.review_entered.set()
        if self.review_release is not None and not self.review_release.wait(1.0):
            raise RuntimeError("synthetic review release deadline exceeded")
        if self.review_failure:
            raise RuntimeError("synthetic uncertain review")
        if self.review_task_status != "awaiting_user_review":
            raise RuntimeError("synthetic duplicate review")
        self.review_task_generation += 1
        self.review_task_status = "succeeded"
        self.review_committed_revision = "revision_" + "4" * 32
        self.review_project_generation += 1
        self.review_project_revision = "revision_" + "4" * 32
        if self.review_effect_after_loss:
            raise RuntimeError("synthetic review response loss after effect")
        return _fake_task_detail(
            "1",
            generation=self.review_task_generation,
            status=self.review_task_status,
            committed_revision=self.review_committed_revision,
        )

    def reject_draft_request(self, request: dict[str, object]) -> dict[str, object]:
        self._record("reject_draft", request)
        if self.review_entered is not None:
            self.review_entered.set()
        if self.review_release is not None and not self.review_release.wait(1.0):
            raise RuntimeError("synthetic review release deadline exceeded")
        if self.review_task_status != "awaiting_user_review":
            raise RuntimeError("synthetic duplicate review")
        self.review_task_generation += 1
        self.review_task_status = "rejected"
        self.review_committed_revision = None
        if self.review_effect_after_loss:
            raise RuntimeError("synthetic review response loss after effect")
        return _fake_task_detail(
            "1",
            generation=self.review_task_generation,
            status=self.review_task_status,
            committed_revision=None,
        )

    def open_checkout(
        self,
        *,
        open_key: str,
        source: dict[str, object],
    ) -> dict[str, object]:
        self._record("open_checkout", {"open_key": open_key, "source": source})
        if self.open_checkout_entered is not None:
            self.open_checkout_entered.set()
        if self.open_checkout_release is not None and not self.open_checkout_release.wait(1.0):
            raise RuntimeError("synthetic preview-open release deadline exceeded")
        if self.monotonic_preview_authorities:
            authority = self._next_preview_authority
            self._next_preview_authority += 1
            identifier = f"{authority:032x}"
        else:
            digit = "6" if source["kind"] == "head" else "7"
            identifier = digit * 32
        checkout_id = "checkout_" + identifier
        grant_id = "file_grant_" + identifier
        path = (self.materialized_checkout_root / checkout_id / "model.FCStd").resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(self.materialized_model_bytes)
        self.checkout_paths[checkout_id] = path
        local_path = str(path)
        model_digest = hashlib.sha256(self.materialized_model_bytes).hexdigest()
        size = len(self.materialized_model_bytes)
        if source["kind"] == "head":
            resolved_project_id = source["project_id"]
        else:
            task_id = source.get("task_id")
            resolved_project_id = self.draft_project_by_task_id.get(
                task_id if type(task_id) is str else "",
                "project_" + "1" * 32,
            )
        head_revision_id = "revision_" + "1" * 32
        candidate_revision_id = "revision_" + "4" * 32
        tasks_response = self.last_tasks_response
        task_records: object = None
        if type(tasks_response) is dict:
            result = tasks_response.get("result")
            if type(result) is dict:
                task_records = result.get("tasks")
        if type(task_records) is list:
            for task in task_records:
                if type(task) is not dict:
                    continue
                matches = (
                    source["kind"] == "draft"
                    and task.get("task_id") == source.get("task_id")
                    or source["kind"] == "head"
                    and task.get("project_id") == source.get("project_id")
                    and task.get("status") == "awaiting_user_review"
                )
                if not matches:
                    continue
                base_revision = task.get("base_revision")
                candidate_revision = task.get("candidate_revision")
                if type(base_revision) is str:
                    head_revision_id = base_revision
                if source["kind"] == "draft" and type(candidate_revision) is str:
                    candidate_revision_id = candidate_revision
                break
        head_manifest_digest = head_revision_id.removeprefix("revision_") * 2
        if source["kind"] == "head":
            resolved_revision_id = head_revision_id
            resolved_manifest_digest = head_manifest_digest
        else:
            resolved_revision_id = candidate_revision_id
            resolved_manifest_digest = candidate_revision_id.removeprefix("revision_") * 2
        resolved_source = {
            "kind": source["kind"],
            "project_id": resolved_project_id,
            "revision_id": resolved_revision_id,
            "manifest_sha256": resolved_manifest_digest,
            "model_sha256": model_digest,
            "size_bytes": size,
            "task_id": source.get("task_id"),
            "draft_id": source.get("draft_id"),
            "task_generation": source.get("expected_generation"),
        }
        response = {
            "checkout_id": checkout_id,
            "open_key": open_key,
            "state": "open",
            "authoritative": False,
            "dirty": False,
            "source": resolved_source,
            "initial_model_sha256": model_digest,
            "current_model_sha256": model_digest,
            "current_size_bytes": size,
            "source_head": {
                "schema_version": 1,
                "project_id": resolved_project_id,
                "generation": 2,
                "revision_id": head_revision_id,
                "manifest_sha256": head_manifest_digest,
            },
            "source_liveness": "live",
            "file_grant": {
                "schema_version": 1,
                "grant_id": grant_id,
                "purpose": "open_managed_checkout",
                "expires_in_ms": 30_000,
            },
        }
        self._preview_grant_claims[grant_id] = {
            "schema_version": 1,
            "grant_id": grant_id,
            "checkout_id": checkout_id,
            "purpose": "open_managed_checkout",
            "local_path": local_path,
            "current_model_sha256": model_digest,
            "current_size_bytes": size,
        }
        transform = self.open_checkout_transform
        if callable(transform):
            response = transform(response)
        if type(response) is dict and type(response.get("checkout_id")) is str:
            descriptor = dict(response)
            descriptor.pop("file_grant", None)
            stored_checkout_id = response["checkout_id"]
            self.checkout_descriptors[stored_checkout_id] = descriptor
            self.checkout_file_observations[stored_checkout_id] = (
                descriptor.get("current_model_sha256"),
                descriptor.get("current_size_bytes"),
                descriptor.get("dirty"),
            )
        return response

    def claim_file_grant(self, *, grant_id: str) -> dict[str, object]:
        self._record("claim_file_grant", {"grant_id": grant_id})
        if self.claim_file_grant_failures:
            self.claim_file_grant_failures -= 1
            raise RuntimeError("synthetic file grant claim failure")
        retained = self._preview_grant_claims.get(grant_id)
        if retained is not None:
            return dict(retained)
        digit = grant_id[-1]
        checkout_id = "checkout_" + digit * 32
        return {
            "schema_version": 1,
            "grant_id": grant_id,
            "checkout_id": checkout_id,
            "purpose": "open_managed_checkout",
            "local_path": f"/managed/checkouts/{checkout_id}/model.FCStd",
            "current_model_sha256": digit * 64,
            "current_size_bytes": 23,
        }

    def close_checkout(self, *, checkout_id: str) -> dict[str, object]:
        self._record("close_checkout", {"checkout_id": checkout_id})
        if self.checkout_close_failures:
            self.checkout_close_failures -= 1
            raise RuntimeError("synthetic checkout close failure")
        if self.checkout_close_responses:
            return self.checkout_close_responses.pop(0)
        self.events.append(f"checkout.close:{checkout_id}")
        descriptor = dict(self.checkout_descriptors[checkout_id])
        descriptor["state"] = "closed"
        return descriptor

    def get_checkout(self, *, checkout_id: str) -> dict[str, object]:
        self._record("get_checkout", {"checkout_id": checkout_id})
        descriptor = dict(self.checkout_descriptors[checkout_id])
        observed = (
            descriptor.get("current_model_sha256"),
            descriptor.get("current_size_bytes"),
            descriptor.get("dirty"),
        )
        if observed == self.checkout_file_observations[checkout_id]:
            path = self.checkout_paths[checkout_id]
            content = path.read_bytes()
            descriptor["current_model_sha256"] = hashlib.sha256(content).hexdigest()
            descriptor["current_size_bytes"] = len(content)
            descriptor["dirty"] = (
                descriptor["current_model_sha256"] != descriptor["initial_model_sha256"]
            )
            self.checkout_file_observations[checkout_id] = (
                descriptor["current_model_sha256"],
                descriptor["current_size_bytes"],
                descriptor["dirty"],
            )
        self.checkout_descriptors[checkout_id] = descriptor
        return dict(descriptor)

    def checkpoint_checkout(
        self,
        *,
        checkpoint_key: str,
        checkout_id: str,
    ) -> dict[str, object]:
        self._record(
            "checkpoint_checkout",
            {"checkpoint_key": checkpoint_key, "checkout_id": checkout_id},
        )
        return {
            "schema_version": 1,
            "generation": 8,
            "next_action": "done",
            "task_run": {
                "id": "task_" + "8" * 32,
                "status": "succeeded",
            },
        }

    def close(self) -> None:
        if threading.get_ident() != self.created_thread_id:
            raise RuntimeError("fake client thread authority violation")
        self.close_call_count += 1
        if self.client_close_failures:
            self.client_close_failures -= 1
            raise RuntimeError("synthetic client close failure")
        self.closed_thread_id = threading.get_ident()
        self.events.append("client.close")
        if self._materialized_checkout_tmp is not None:
            self._materialized_checkout_tmp.cleanup()
            self._materialized_checkout_tmp = None


def _fake_task(digit: str, *, status: str) -> dict[str, object]:
    return {
        "task_id": "task_" + digit * 32,
        "project_id": "project_" + "1" * 32,
        "generation": 3,
        "base_revision": "revision_" + digit * 32,
        "reasoning_owner": "server",
        "review_policy": "required",
        "status": status,
        "next_action": "review",
        "candidate_revision": "revision_" + "4" * 32,
        "committed_revision": None,
        "draft_id": "draft_" + "4" * 32,
    }


def _fake_task_detail(
    digit: str,
    *,
    generation: int = 3,
    status: str,
    committed_revision: str | None = None,
) -> dict[str, object]:
    summary = _fake_task(digit, status=status)
    task_id = summary["task_id"]
    project_id = summary["project_id"]
    base_revision = summary["base_revision"]
    candidate_revision = summary["candidate_revision"]
    draft_id = summary["draft_id"]
    assert type(task_id) is str
    assert type(project_id) is str
    assert type(base_revision) is str
    assert type(candidate_revision) is str
    assert type(draft_id) is str
    return {
        "schema_version": 1,
        "ok": True,
        "result": {
            "generation": generation,
            "next_action": "review" if status == "awaiting_user_review" else "none",
            "task_run": {
                "schema_version": 1,
                "id": task_id,
                "project_id": project_id,
                "base_revision": base_revision,
                "reasoning_owner": summary["reasoning_owner"],
                "review_policy": summary["review_policy"],
                "status": status,
                "creation_digest": "a" * 64,
                "program": None,
                "candidate_revision": candidate_revision,
                "committed_revision": committed_revision,
                "draft": {
                    "schema_version": 1,
                    "id": draft_id,
                    "task_id": task_id,
                    "project_id": project_id,
                    "base_revision": base_revision,
                    "base_generation": 2,
                    "base_manifest_sha256": "b" * 64,
                    "revision_id": candidate_revision,
                    "manifest_sha256": "c" * 64,
                    "verification_id": "verification_" + "d" * 32,
                    "acceptance_id": "acceptance-c03",
                    "observation_digest": "e" * 64,
                },
                "steps": [],
                "verification_reports": [],
                "artifacts": [],
                "last_error": None,
                "transitions": [],
            },
        },
        "error": None,
    }


def _fake_project_detail(*, generation: int, revision_id: str) -> dict[str, object]:
    project_id = "project_" + "1" * 32
    manifest = ("1" if revision_id == "revision_" + "1" * 32 else "4") * 64
    model = {
        "schema_version": 1,
        "id": "artifact_" + "6" * 32,
        "name": "model.FCStd",
        "format": "fcstd",
        "sha256": "7" * 64,
        "size_bytes": 23,
    }
    return {
        "schema_version": 1,
        "ok": True,
        "result": {
            "schema_version": 1,
            "project_id": project_id,
            "current": {
                "head": {
                    "schema_version": 1,
                    "project_id": project_id,
                    "generation": generation,
                    "revision_id": revision_id,
                    "manifest_sha256": manifest,
                },
                "revision": {
                    "schema_version": 1,
                    "id": revision_id,
                    "project_id": project_id,
                    "base_revision": (
                        "revision_" + "0" * 32
                        if revision_id == "revision_" + "1" * 32
                        else "revision_" + "1" * 32
                    ),
                    "manifest_sha256": manifest,
                    "model": model,
                    "artifacts": [],
                },
            },
        },
        "error": None,
    }


class _BoundSignal:
    def __init__(self, owner: object) -> None:
        self._owner = owner
        self._connections: list[tuple[object, object]] = []

    def connect(self, slot: object, connection_type: object = None) -> None:
        self._connections.append((slot, connection_type))

    def emit(self, *args: object) -> None:
        for slot, connection_type in tuple(self._connections):
            receiver = getattr(slot, "__self__", None)
            target = getattr(receiver, "_qt_thread", None)
            if connection_type == _Qt.ConnectionType.QueuedConnection:
                if isinstance(target, _QThread):
                    target.post(slot, args)
                else:
                    _MAIN_EVENTS.put((slot, args))
            else:
                slot(*args)


class _Signal:
    def __init__(self, *_types: object) -> None:
        self._name = ""

    def __set_name__(self, _owner: type[object], name: str) -> None:
        self._name = f"__signal_{name}"

    def __get__(self, instance: object, _owner: type[object]) -> object:
        if instance is None:
            return self
        signal = getattr(instance, self._name, None)
        if signal is None:
            signal = _BoundSignal(instance)
            setattr(instance, self._name, signal)
        return signal


class _QObject:
    def __init__(self, parent: object = None) -> None:
        self._qt_thread: _QThread | None = None
        self._qt_parent: object | None = None
        self._qt_children: list[object] = []
        self.delete_scheduled = False
        self.deleted = False
        if parent is not None:
            self.setParent(parent)

    def moveToThread(self, thread: _QThread) -> None:
        self._qt_thread = thread

    def setParent(self, parent: object | None) -> None:
        previous = self._qt_parent
        if previous is parent:
            return
        if previous is not None:
            previous_children = previous._qt_children
            previous_children[:] = [child for child in previous_children if child is not self]
        self._qt_parent = parent
        if parent is not None:
            parent._qt_children.append(self)

    def parent(self) -> object | None:
        return self._qt_parent

    def children(self) -> list[object]:
        return list(self._qt_children)

    def findChildren(
        self,
        child_type: type[object],
        name: str | None = None,
    ) -> list[object]:
        return _find_qt_children(self, child_type, name)

    def deleteLater(self) -> None:
        self.delete_scheduled = True


class _QThread(_QObject):
    finished = _Signal()

    def __init__(self) -> None:
        super().__init__()
        self._events: queue.Queue[object] = queue.Queue()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="fake-qt-worker")
        self._thread.start()

    def _run(self) -> None:
        while True:
            event = self._events.get()
            if event is None:
                break
            slot, args = event
            slot(*args)
        self.finished.emit()

    def post(self, slot: object, args: tuple[object, ...]) -> None:
        self._events.put((slot, args))

    def quit(self) -> None:
        self._events.put(None)

    def isRunning(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def join(self, timeout: float = 1.0) -> None:
        if self._thread is not None:
            self._thread.join(timeout)


class _QTimer(_QObject):
    timeout = _Signal()

    def __init__(self, parent: object = None) -> None:
        super().__init__(parent)
        self.interval = 0
        self.active = False

    def setInterval(self, interval: int) -> None:
        self.interval = interval

    def start(self) -> None:
        self.active = True

    def stop(self) -> None:
        self.active = False

    def isActive(self) -> bool:
        return self.active


class _Widget(_QObject):
    def __init__(self, *_args: object) -> None:
        _require_main_thread()
        super().__init__(*_args)
        self.object_name = ""
        self.hidden = False

    def setObjectName(self, name: str) -> None:
        _require_main_thread()
        self.object_name = name

    def objectName(self) -> str:
        _require_main_thread()
        return self.object_name

    def setParent(self, parent: object | None) -> None:
        _require_main_thread()
        super().setParent(parent)

    def hide(self) -> None:
        _require_main_thread()
        self.hidden = True


class _QDockWidget(_Widget):
    def __init__(self, title: str, parent: object = None) -> None:
        super().__init__(parent)
        self.title = title
        self.widget: object | None = None

    def setWidget(self, widget: object) -> None:
        _require_main_thread()
        self.widget = widget


class _QWidget(_Widget):
    pass


class _QVBoxLayout:
    def __init__(self, parent: object) -> None:
        _require_main_thread()
        self.parent = parent
        self.widgets: list[object] = []

    def addWidget(self, widget: object) -> None:
        _require_main_thread()
        self.widgets.append(widget)


class _QLabel(_Widget):
    def __init__(self, text: str, parent: object) -> None:
        super().__init__(parent)
        self.text = text

    def setText(self, text: str) -> None:
        _require_main_thread()
        self.text = text


class _QComboBox(_Widget):
    currentIndexChanged = _Signal(int)

    def __init__(self, parent: object) -> None:
        super().__init__(parent)
        self.items: list[str] = []
        self.index = -1
        self.blocked = False

    def clear(self) -> None:
        _require_main_thread()
        self.items.clear()
        self.setCurrentIndex(-1)

    def addItem(self, item: str) -> None:
        _require_main_thread()
        self.items.append(item)
        if len(self.items) == 1:
            self.setCurrentIndex(0)

    def currentIndex(self) -> int:
        _require_main_thread()
        return self.index

    def setCurrentIndex(self, index: int) -> None:
        _require_main_thread()
        if self.index == index:
            return
        self.index = index
        if not self.blocked:
            self.currentIndexChanged.emit(index)

    def blockSignals(self, blocked: bool) -> None:
        _require_main_thread()
        self.blocked = blocked


class _QPushButton(_Widget):
    clicked = _Signal()

    def __init__(self, text: str, parent: object) -> None:
        super().__init__(parent)
        self.text = text
        self.enabled = True

    def setEnabled(self, enabled: bool) -> None:
        _require_main_thread()
        self.enabled = enabled is True

    def click(self) -> None:
        _require_main_thread()
        if self.enabled:
            self.clicked.emit()


class _Clipboard:
    def __init__(self) -> None:
        self.text = ""

    def setText(self, text: str) -> None:
        _require_main_thread()
        self.text = text


class _QApplication:
    _clipboard = _Clipboard()

    @classmethod
    def instance(cls) -> type[_QApplication]:
        return cls

    @classmethod
    def clipboard(cls) -> _Clipboard:
        return cls._clipboard


class _ConnectionType:
    QueuedConnection = object()


class _DockWidgetArea:
    RightDockWidgetArea = object()


class _QtNestedOnly:
    ConnectionType = _ConnectionType
    DockWidgetArea = _DockWidgetArea


class _QtFlatOnly:
    QueuedConnection = _ConnectionType.QueuedConnection
    RightDockWidgetArea = _DockWidgetArea.RightDockWidgetArea


class _Qt(_QtNestedOnly):
    QueuedConnection = _ConnectionType.QueuedConnection
    RightDockWidgetArea = _DockWidgetArea.RightDockWidgetArea


_MAIN_EVENTS: queue.Queue[tuple[object, tuple[object, ...]]] = queue.Queue()


def install_fake_pyside(
    *,
    nested_only: bool = False,
    flat_only: bool = False,
) -> ModuleType:
    if nested_only and flat_only:
        raise ValueError("fake Qt enum shape is ambiguous")
    module = ModuleType("PySide")
    module.QtCore = ModuleType("PySide.QtCore")
    module.QtCore.QObject = _QObject
    module.QtCore.QThread = _QThread
    module.QtCore.QTimer = _QTimer
    module.QtCore.Signal = _Signal
    module.QtCore.Slot = lambda *_args: lambda function: function
    if nested_only:
        module.QtCore.Qt = _QtNestedOnly
    elif flat_only:
        module.QtCore.Qt = _QtFlatOnly
    else:
        module.QtCore.Qt = _Qt
    module.QtWidgets = ModuleType("PySide.QtWidgets")
    module.QtWidgets.QDockWidget = _QDockWidget
    module.QtWidgets.QWidget = _QWidget
    module.QtWidgets.QVBoxLayout = _QVBoxLayout
    module.QtWidgets.QLabel = _QLabel
    module.QtWidgets.QComboBox = _QComboBox
    module.QtWidgets.QPushButton = _QPushButton
    _QApplication._clipboard = _Clipboard()
    module.QtWidgets.QApplication = _QApplication
    return module


def pump_main_events(
    predicate: object,
    *,
    timeout: float = 1.0,
) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise AssertionError("fake Qt event deadline exceeded")
        try:
            slot, args = _MAIN_EVENTS.get(timeout=min(remaining, 0.05))
        except queue.Empty:
            continue
        slot(*args)


def _take_owned_main_event(owner: object) -> tuple[object, tuple[object, ...]] | None:
    with _MAIN_EVENTS.mutex:
        for index, item in enumerate(_MAIN_EVENTS.queue):
            slot, _args = item
            if getattr(slot, "__self__", None) is owner:
                del _MAIN_EVENTS.queue[index]
                return item
    return None


def _discard_owned_main_events(owner: object) -> None:
    while _take_owned_main_event(owner) is not None:
        pass


def settle_workbench_events(
    session: object,
    predicate: object,
    *,
    maximum_rounds: int = 16,
) -> None:
    """Cross each worker/main FIFO boundary without a multi-hop wall-clock race."""
    if not callable(predicate) or type(maximum_rounds) is not int or maximum_rounds <= 0:
        raise TypeError("invalid fake Qt settlement request")
    thread = getattr(session, "thread", None)
    if thread is None:
        raise AssertionError("fake Qt session has no worker thread")
    for _round in range(maximum_rounds):
        if predicate():
            return
        if thread.isRunning():
            if getattr(session, "_retirement_authorized", False):
                thread.join()
            else:
                settled = threading.Event()
                thread.post(settled.set, ())
                if not settled.wait(1.0):
                    raise AssertionError("fake Qt worker barrier deadline exceeded")
        progressed = False
        while True:
            item = _take_owned_main_event(session)
            if item is None:
                break
            progressed = True
            slot, args = item
            slot(*args)
            if predicate():
                return
        if not thread.isRunning() and not progressed:
            break
    raise AssertionError("fake Qt lifecycle did not settle")


def _release_fake_client_waits(session: object) -> None:
    worker = getattr(session, "worker", None)
    gateway = getattr(worker, "gateway", None)
    client = getattr(gateway, "_client", None)
    for name in ("open_checkout_release", "review_release"):
        release = getattr(client, name, None)
        if isinstance(release, threading.Event):
            release.set()


def force_cleanup_workbench(host: object, freecad: FakeFreeCAD) -> None:
    """Retire only the fake session owned by one failure-injection test."""
    session = getattr(host, "_session", None)
    if session is None:
        freecad.documents.clear()
        return
    thread = getattr(session, "thread", None)
    if thread is not None and thread.isRunning():
        if getattr(session, "_close_request_id", None) is None:
            session._queue_partial_close()
        try:
            settle_workbench_events(
                session,
                lambda: getattr(host, "_session", None) is None,
            )
        except AssertionError:
            _release_fake_client_waits(session)
            thread.quit()
            thread.join()
    if thread is not None and thread.isRunning():
        raise AssertionError("fake Qt worker thread did not retire")
    _discard_owned_main_events(session)
    if getattr(host, "_session", None) is session:
        session._detach_dock()
        session.lifecycle = "inactive"
        session._thread_retired = True
        host._session = None
    freecad.documents.clear()
