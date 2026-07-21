"""Store-only task catalog contracts."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from vibecad.execution.revisions import (
    LocalRevisionStore,
    RevisionStoreRootTrust,
)
from vibecad.workflow.catalog import (
    TaskCatalogError,
    TaskCatalogErrorCode,
    TaskCatalogService,
)
from vibecad.workflow.lease import LeaseRootTrust, ResourceLeaseManager
from vibecad.workflow.state import ReasoningOwner, ReviewPolicy, TaskStatus
from vibecad.workflow.store import TaskRunStore, TaskStoreRootTrust

TASK_ID = "task_0123456789abcdef0123456789abcdef"
PROJECT_ID = "project_0123456789abcdef0123456789abcdef"


def _stores(tmp_path: Path):
    locks = tmp_path / "locks"
    tasks = tmp_path / "tasks"
    projects = tmp_path / "projects"
    for root in (locks, tasks, projects):
        root.mkdir(mode=0o700)
        os.chmod(root, 0o700)
    leases = ResourceLeaseManager(locks, trust=LeaseRootTrust.TRUSTED_LOCAL)
    task_store = TaskRunStore(tasks, leases, trust=TaskStoreRootTrust.TRUSTED_LOCAL)
    revision_store = LocalRevisionStore(
        projects,
        leases,
        trust=RevisionStoreRootTrust.TRUSTED_LOCAL,
    )
    with leases.acquire_project_write(PROJECT_ID) as lease:
        head = revision_store.initialize_empty_project(PROJECT_ID, lease)
    return leases, task_store, revision_store, head


def test_catalog_creates_and_gets_a_task_without_any_cad_port(tmp_path: Path):
    _leases, tasks, revisions, head = _stores(tmp_path)
    catalog = TaskCatalogService(task_store=tasks, revision_store=revisions)

    created = catalog.create_task(
        task_id=TASK_ID,
        project_id=PROJECT_ID,
        reasoning_owner=ReasoningOwner.EXTERNAL_PLAN,
        review_policy=ReviewPolicy.REQUIRE_REVIEW,
    )

    assert created.generation == 0
    assert created.task_run.status is TaskStatus.NEEDS_PLAN
    assert created.task_run.base_revision == head.revision_id
    assert catalog.get_task(task_id=TASK_ID) == created
    assert set(TaskCatalogService.__dict__) >= {
        "create_task",
        "get_task",
        "reject_draft",
    }


def test_catalog_errors_are_closed_and_path_free():
    for code in TaskCatalogErrorCode:
        error = TaskCatalogError(code)
        assert error.code is code
        assert error.to_mapping()["code"] == code.value
        assert "path" not in json.dumps(error.to_mapping())


def test_fresh_catalog_import_and_create_do_not_load_cad_modules():
    script = f"""
import json
import os
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from vibecad.execution.revisions import LocalRevisionStore, RevisionStoreRootTrust
from vibecad.workflow.catalog import TaskCatalogService
from vibecad.workflow.lease import LeaseRootTrust, ResourceLeaseManager
from vibecad.workflow.state import ReasoningOwner, ReviewPolicy
from vibecad.workflow.store import TaskRunStore, TaskStoreRootTrust

with TemporaryDirectory() as value:
    root = Path(value).resolve()
    roots = [root / name for name in ('locks', 'tasks', 'projects')]
    for item in roots:
        item.mkdir(mode=0o700)
        os.chmod(item, 0o700)
    leases = ResourceLeaseManager(roots[0], trust=LeaseRootTrust.TRUSTED_LOCAL)
    tasks = TaskRunStore(roots[1], leases, trust=TaskStoreRootTrust.TRUSTED_LOCAL)
    revisions = LocalRevisionStore(
        roots[2], leases, trust=RevisionStoreRootTrust.TRUSTED_LOCAL
    )
    with leases.acquire_project_write('{PROJECT_ID}') as lease:
        revisions.initialize_empty_project('{PROJECT_ID}', lease)
    TaskCatalogService(task_store=tasks, revision_store=revisions).create_task(
        task_id='{TASK_ID}', project_id='{PROJECT_ID}',
        reasoning_owner=ReasoningOwner.EXTERNAL_PLAN,
        review_policy=ReviewPolicy.AUTO_COMMIT,
    )
forbidden = ('FreeCAD', 'Part', 'vibecad.engine', 'vibecad.tools',
             'vibecad.execution.executor', 'vibecad.execution.candidate')
loaded = sorted(name for name in sys.modules if any(
    name == prefix or name.startswith(prefix + '.') for prefix in forbidden
))
assert loaded == [], json.dumps(loaded)
"""
    completed = subprocess.run(
        [sys.executable, "-c", script],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr or completed.stdout
