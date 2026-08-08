"""Thin public application and Workbench adapter over the local Kernel."""

from __future__ import annotations

import contextlib
import hashlib
import os
import re
import secrets
import threading
from enum import StrEnum
from pathlib import Path

from vibecad import __version__
from vibecad.application.project_api import (
    ProjectApi,
    ProjectKind,
    ProjectServicePortErrorCode,
    ProjectServicePortFailure,
)
from vibecad.application.visual_ingress import (
    VisualIngressError,
    parse_seal_image_set_request,
    validate_seal_result,
)
from vibecad.daemon.bootstrap import (
    connect_existing_local_kernel,
    connect_or_start_local_kernel,
    retire_local_kernel,
)
from vibecad.daemon.client import LocalImportSourceError, LocalVisualSourceError
from vibecad.daemon.facade import (
    KERNEL_API_EPOCH,
    KERNEL_API_NAME,
    KERNEL_BUILD_ID,
)
from vibecad.daemon.state import DaemonError
from vibecad.interaction.protocol_v2 import V2_VERSION
from vibecad.runtime import paths

_GRANT_RE = re.compile(r"file_grant_[0-9a-f]{32}\Z")


class _ProjectCreatePreflight:
    """Pure ProjectApi port: capture validated intent without touching state."""

    __slots__ = ("create_key", "kind", "source_path")

    def __init__(self) -> None:
        self.create_key: str | None = None
        self.kind: ProjectKind | None = None
        self.source_path: str | None = None

    def create_project(
        self,
        *,
        create_key: str,
        kind: ProjectKind,
        source_path: str | None,
    ) -> ProjectServicePortFailure:
        self.create_key = create_key
        self.kind = kind
        self.source_path = source_path
        return ProjectServicePortFailure(
            code=ProjectServicePortErrorCode.INVALID_INPUT,
        )


class LocalAgentClientErrorCode(StrEnum):
    INVALID_INPUT = "invalid_input"
    RESOURCE_EXHAUSTED = "resource_exhausted"
    UNAVAILABLE = "unavailable"
    INTERNAL_ERROR = "internal_error"
    CLOSED = "closed"
    WRONG_PROCESS = "wrong_process"
    INCOMPATIBLE_KERNEL = "incompatible_kernel"


class LocalAgentClientError(RuntimeError):
    __slots__ = ("code",)

    def __init__(self, code: LocalAgentClientErrorCode) -> None:
        if type(code) is not LocalAgentClientErrorCode:
            raise TypeError("code must be a LocalAgentClientErrorCode")
        self.code = code
        super().__init__(code.value)


class LocalAgentClient:
    """One connection-bound client for MCP and FreeCAD Workbench adapters."""

    __slots__ = (
        "_artifact_root",
        "_closed",
        "_kernel",
        "_kernel_lock",
        "_pid",
        "_reconnectable",
        "_release_root",
        "_visual_review_root",
    )

    def __init__(
        self,
        kernel: object,
        *,
        artifact_root: object | None = None,
        release_root: object | None = None,
        visual_review_root: object | None = None,
        reconnectable: bool = False,
    ) -> None:
        if not callable(getattr(kernel, "call", None)) or not callable(
            getattr(kernel, "close", None)
        ):
            raise TypeError("kernel must be a local Kernel client")
        if artifact_root is None:
            canonical_artifact_root = None
        elif type(artifact_root) is str:
            canonical_artifact_root = Path(artifact_root)
        elif type(artifact_root) is type(Path("/")):
            canonical_artifact_root = artifact_root
        else:
            raise TypeError("artifact_root must be an absolute path")
        if canonical_artifact_root is not None and (
            not canonical_artifact_root.is_absolute() or ".." in canonical_artifact_root.parts
        ):
            raise TypeError("artifact_root must be an absolute path")
        if release_root is None:
            canonical_release_root = None
        elif type(release_root) is str:
            canonical_release_root = Path(release_root)
        elif type(release_root) is type(Path("/")):
            canonical_release_root = release_root
        else:
            raise TypeError("release_root must be an absolute path")
        if canonical_release_root is not None and (
            not canonical_release_root.is_absolute() or ".." in canonical_release_root.parts
        ):
            raise TypeError("release_root must be an absolute path")
        if visual_review_root is None:
            canonical_visual_review_root = None
        elif type(visual_review_root) is str:
            canonical_visual_review_root = Path(visual_review_root)
        elif type(visual_review_root) is type(Path("/")):
            canonical_visual_review_root = visual_review_root
        else:
            raise TypeError("visual_review_root must be an absolute path")
        if canonical_visual_review_root is not None and (
            not canonical_visual_review_root.is_absolute()
            or ".." in canonical_visual_review_root.parts
        ):
            raise TypeError("visual_review_root must be an absolute path")
        if type(reconnectable) is not bool:
            raise TypeError("reconnectable must be a bool")
        self._kernel = kernel
        self._kernel_lock = threading.RLock()
        self._reconnectable = reconnectable
        self._artifact_root = canonical_artifact_root
        self._release_root = canonical_release_root
        self._visual_review_root = canonical_visual_review_root
        self._closed = False
        self._pid = os.getpid()

    @classmethod
    def open(cls) -> LocalAgentClient:
        client = cls(
            connect_or_start_local_kernel(),
            artifact_root=paths.data_root() / "artifacts",
            release_root=paths.data_root() / "releases",
            visual_review_root=paths.visual_review_root(),
            reconnectable=True,
        )
        observed_daemon_id = client.daemon_id
        try:
            return client._verified()
        except LocalAgentClientError as error:
            if error.code is not LocalAgentClientErrorCode.INCOMPATIBLE_KERNEL:
                raise
        retire_local_kernel(
            reason="incompatible_build",
            expected_daemon_id=observed_daemon_id,
        )
        replacement = cls(
            connect_or_start_local_kernel(),
            artifact_root=paths.data_root() / "artifacts",
            release_root=paths.data_root() / "releases",
            visual_review_root=paths.visual_review_root(),
            reconnectable=True,
        )
        return replacement._verified()

    @classmethod
    def connect(
        cls,
        run_root: object,
        *,
        artifact_root: object | None = None,
        release_root: object | None = None,
        visual_review_root: object | None = None,
    ) -> LocalAgentClient:
        client = cls(
            connect_existing_local_kernel(run_root),
            artifact_root=artifact_root,
            release_root=release_root,
            visual_review_root=visual_review_root,
        )
        return client._verified()

    def _verified(self) -> LocalAgentClient:
        try:
            result = self.ping()
            if (
                result.get("schema_version") != 1
                or result.get("daemon_id") != self.daemon_id
                or result.get("status") != "ready"
                or result.get("protocol") != {"major": V2_VERSION[0], "minor": V2_VERSION[1]}
                or result.get("api")
                != {
                    "name": KERNEL_API_NAME,
                    "epoch": KERNEL_API_EPOCH,
                }
                or result.get("implementation")
                != {
                    "package_version": __version__,
                    "build_id": KERNEL_BUILD_ID,
                }
            ):
                raise LocalAgentClientError(LocalAgentClientErrorCode.INCOMPATIBLE_KERNEL)
            return self
        except BaseException:
            with contextlib.suppress(BaseException):
                self.close()
            raise

    @property
    def daemon_id(self) -> str:
        self._ensure_live()
        value = getattr(self._kernel, "daemon_id", None)
        if type(value) is not str:
            raise LocalAgentClientError(LocalAgentClientErrorCode.INTERNAL_ERROR)
        return value

    def _ensure_live(self) -> None:
        if os.getpid() != self._pid:
            raise LocalAgentClientError(LocalAgentClientErrorCode.WRONG_PROCESS)
        if self._closed:
            raise LocalAgentClientError(LocalAgentClientErrorCode.CLOSED)

    @staticmethod
    def _result(response: object) -> dict[str, object]:
        result = getattr(response, "result", None)
        error = getattr(response, "error", None)
        if type(result) is dict and error is None:
            return result
        if result is None and type(error) is dict:
            raise LocalAgentClientError(LocalAgentClientErrorCode.UNAVAILABLE)
        raise LocalAgentClientError(LocalAgentClientErrorCode.INTERNAL_ERROR)

    def ping(self) -> dict[str, object]:
        self._ensure_live()
        return self._result(self._kernel.call("kernel.ping", {}))

    def _application_call(
        self,
        operation: str,
        request: object,
    ) -> dict[str, object]:
        if type(request) is not dict:
            raise LocalAgentClientError(LocalAgentClientErrorCode.INVALID_INPUT)
        params = {
            "operation": operation,
            "request": request,
        }
        with self._kernel_lock:
            self._ensure_live()
            try:
                response = self._kernel.call("application.call", params)
            except DaemonError:
                if not self._reconnectable:
                    raise LocalAgentClientError(LocalAgentClientErrorCode.UNAVAILABLE) from None
                old_kernel = self._kernel
                try:
                    replacement = type(self).open()
                except (DaemonError, LocalAgentClientError):
                    raise LocalAgentClientError(LocalAgentClientErrorCode.UNAVAILABLE) from None
                self._kernel = replacement._kernel
                replacement._closed = True
                with contextlib.suppress(BaseException):
                    old_kernel.close()
                try:
                    response = self._kernel.call("application.call", params)
                except DaemonError:
                    raise LocalAgentClientError(LocalAgentClientErrorCode.UNAVAILABLE) from None
            return self._result(response)

    def create_project_request(self, request: object) -> dict[str, object]:
        preflight = _ProjectCreatePreflight()
        fallback = ProjectApi(port=preflight).create_project(request)
        if preflight.create_key is None or preflight.kind is None:
            return fallback
        wire_request = {
            "schema_version": 1,
            "create_key": preflight.create_key,
            "kind": preflight.kind.value,
        }
        if preflight.kind is not ProjectKind.IMPORT_FCSTD:
            return self._application_call("create_project", wire_request)
        import_project = getattr(self._kernel, "import_project", None)
        if not callable(import_project):
            raise LocalAgentClientError(LocalAgentClientErrorCode.UNAVAILABLE)
        self._ensure_live()
        try:
            return self._result(
                import_project(
                    wire_request,
                    source_path=preflight.source_path,
                )
            )
        except LocalImportSourceError:
            return fallback

    def get_project_request(self, request: object) -> dict[str, object]:
        return self._application_call("get_project", request)

    def list_projects_request(self, request: object) -> dict[str, object]:
        return self._application_call("list_projects", request)

    def list_revisions_request(self, request: object) -> dict[str, object]:
        return self._application_call("list_revisions", request)

    def compare_revisions_request(self, request: object) -> dict[str, object]:
        return self._application_call("compare_revisions", request)

    def revert_project_request(self, request: object) -> dict[str, object]:
        return self._application_call("revert_project", request)

    def create_task_request(self, request: object) -> dict[str, object]:
        return self._application_call("create_task", request)

    def list_tasks_request(self, request: object) -> dict[str, object]:
        return self._application_call("list_tasks", request)

    def get_task_request(self, request: object) -> dict[str, object]:
        return self._application_call("get_task", request)

    def get_task_events_request(self, request: object) -> dict[str, object]:
        return self._application_call("get_task_events", request)

    def submit_model_program_request(self, request: object) -> dict[str, object]:
        return self._application_call("submit_model_program", request)

    def resume_task_request(self, request: object) -> dict[str, object]:
        return self._application_call("resume_task", request)

    def cancel_task_request(self, request: object) -> dict[str, object]:
        return self._application_call("cancel_task", request)

    def accept_draft_request(self, request: object) -> dict[str, object]:
        return self._application_call("accept_draft", request)

    def reject_draft_request(self, request: object) -> dict[str, object]:
        return self._application_call("reject_draft", request)

    def get_artifact_manifest_request(self, request: object) -> dict[str, object]:
        return self._application_call("get_artifact_manifest", request)

    def export_task_artifacts_request(self, request: object) -> dict[str, object]:
        return self._application_call("export_task_artifacts", request)

    def create_release_request(self, request: object) -> dict[str, object]:
        return self._application_call("create_release", request)

    def get_release_request(self, request: object) -> dict[str, object]:
        return self._application_call("get_release", request)

    def approve_release_request(self, request: object) -> dict[str, object]:
        return self._application_call("approve_release", request)

    def create_reconstruction_request(self, request: object) -> dict[str, object]:
        return self._application_call("create_reconstruction", request)

    def get_reconstruction_request(self, request: object) -> dict[str, object]:
        return self._application_call("get_reconstruction", request)

    def run_reconstruction_request(self, request: object) -> dict[str, object]:
        return self._application_call("run_reconstruction", request)

    def answer_reconstruction_request(self, request: object) -> dict[str, object]:
        return self._application_call("answer_reconstruction", request)

    def adopt_reconstruction_request(self, request: object) -> dict[str, object]:
        return self._application_call("adopt_reconstruction", request)

    def reject_reconstruction_request(self, request: object) -> dict[str, object]:
        return self._application_call("reject_reconstruction", request)

    def delete_reconstruction_request(self, request: object) -> dict[str, object]:
        return self._application_call("delete_reconstruction", request)

    def seal_visual_image_set_request(
        self,
        request: object,
        *,
        source_paths: object,
    ) -> dict[str, object]:
        """Host-only local-image ingress; paths never enter MCP or the wire."""

        self._ensure_live()
        try:
            canonical = parse_seal_image_set_request(request)
        except VisualIngressError:
            raise LocalAgentClientError(LocalAgentClientErrorCode.INVALID_INPUT) from None
        if type(source_paths) not in {list, tuple} or any(
            type(path) is not str for path in source_paths
        ):
            raise LocalAgentClientError(LocalAgentClientErrorCode.INVALID_INPUT)
        paths = tuple(source_paths)
        if len(paths) != len(canonical.inputs):
            raise LocalAgentClientError(LocalAgentClientErrorCode.INVALID_INPUT)
        seal = getattr(self._kernel, "seal_visual_image_set", None)
        if not callable(seal):
            raise LocalAgentClientError(LocalAgentClientErrorCode.UNAVAILABLE)
        try:
            response = seal(
                canonical.to_mapping(),
                source_paths=paths,
            )
        except LocalVisualSourceError:
            raise LocalAgentClientError(LocalAgentClientErrorCode.INVALID_INPUT) from None
        except DaemonError:
            raise LocalAgentClientError(LocalAgentClientErrorCode.UNAVAILABLE) from None
        result = getattr(response, "result", None)
        error = getattr(response, "error", None)
        if type(result) is dict and error is None:
            try:
                return validate_seal_result(result)
            except VisualIngressError:
                raise LocalAgentClientError(LocalAgentClientErrorCode.INTERNAL_ERROR) from None
        if result is None and type(error) is dict and set(error) == {"code", "message"}:
            code = error.get("code")
            if code == "invalid_request":
                raise LocalAgentClientError(LocalAgentClientErrorCode.INVALID_INPUT)
            if code == "resource_exhausted":
                raise LocalAgentClientError(LocalAgentClientErrorCode.RESOURCE_EXHAUSTED)
            raise LocalAgentClientError(LocalAgentClientErrorCode.UNAVAILABLE)
        raise LocalAgentClientError(LocalAgentClientErrorCode.INTERNAL_ERROR)

    def get_capabilities_request(self, request: object) -> dict[str, object]:
        return self._application_call("get_capabilities", request)

    def invoke_direct_operation_request(
        self,
        operation: object,
        request: object,
    ) -> dict[str, object]:
        if type(operation) is not str:
            raise LocalAgentClientError(LocalAgentClientErrorCode.INVALID_INPUT)
        return self._application_call(operation, request)

    def open_checkout(
        self,
        *,
        open_key: object,
        source: object,
    ) -> dict[str, object]:
        self._ensure_live()
        return self._result(
            self._kernel.call(
                "checkout.open",
                {"open_key": open_key, "source": source},
            )
        )

    def get_checkout(self, *, checkout_id: object) -> dict[str, object]:
        self._ensure_live()
        return self._result(self._kernel.call("checkout.get", {"checkout_id": checkout_id}))

    def checkpoint_checkout(
        self,
        *,
        checkpoint_key: object,
        checkout_id: object,
    ) -> dict[str, object]:
        self._ensure_live()
        return self._result(
            self._kernel.call(
                "checkout.checkpoint",
                {
                    "checkpoint_key": checkpoint_key,
                    "checkout_id": checkout_id,
                },
            )
        )

    def close_checkout(self, *, checkout_id: object) -> dict[str, object]:
        self._ensure_live()
        return self._result(self._kernel.call("checkout.close", {"checkout_id": checkout_id}))

    def claim_file_grant(self, *, grant_id: object) -> dict[str, object]:
        if type(grant_id) is not str or _GRANT_RE.fullmatch(grant_id) is None:
            raise LocalAgentClientError(LocalAgentClientErrorCode.INVALID_INPUT)
        self._ensure_live()
        return self._result(self._kernel.call("file_grant.claim", {"grant_id": grant_id}))

    def read_artifact_resource(self, uri: object):
        self._ensure_live()
        if self._artifact_root is None:
            raise LocalAgentClientError(LocalAgentClientErrorCode.UNAVAILABLE)
        try:
            from vibecad.application.artifacts import ArtifactResourceReader

            reader = ArtifactResourceReader(root=self._artifact_root)
            try:
                return reader.read_resource(uri)
            finally:
                close = getattr(reader, "close", None)
                if callable(close):
                    close()
        except ImportError:
            raise LocalAgentClientError(LocalAgentClientErrorCode.UNAVAILABLE) from None

    def read_release_resource(self, uri: object):
        self._ensure_live()
        if self._release_root is None:
            raise LocalAgentClientError(LocalAgentClientErrorCode.UNAVAILABLE)
        try:
            from vibecad.application.releases import ReleaseStore

            value = self._release_root.lstat()
            return ReleaseStore(
                root=self._release_root,
                expected_identity=(value.st_dev, value.st_ino),
            ).read_resource(uri)
        except ImportError:
            raise LocalAgentClientError(LocalAgentClientErrorCode.UNAVAILABLE) from None

    def read_visual_review_resource(self, uri: object):
        self._ensure_live()
        if self._visual_review_root is None:
            raise LocalAgentClientError(LocalAgentClientErrorCode.UNAVAILABLE)
        try:
            from vibecad.visual.review_store import VisualReviewArtifactStore
            from vibecad.workflow.lease import LeaseRootTrust, ResourceLeaseManager

            value = self._visual_review_root.lstat()
            lock_root = self._visual_review_root.parent / "locks"
            return VisualReviewArtifactStore(
                root=self._visual_review_root,
                expected_root_identity=(value.st_dev, value.st_ino),
                lease_manager=ResourceLeaseManager(
                    lock_root,
                    trust=LeaseRootTrust.TRUSTED_LOCAL,
                ),
            ).read_resource(uri)
        except ImportError:
            raise LocalAgentClientError(LocalAgentClientErrorCode.UNAVAILABLE) from None

    def save_release_resource(self, *, uri: object, destination: object) -> dict[str, object]:
        self._ensure_live()
        if type(destination) is not str:
            raise LocalAgentClientError(LocalAgentClientErrorCode.INVALID_INPUT)
        target = Path(destination)
        if not target.is_absolute() or ".." in target.parts or not target.name:
            raise LocalAgentClientError(LocalAgentClientErrorCode.INVALID_INPUT)
        resource = self.read_release_resource(uri)
        temporary = target.parent / f".{target.name}.{secrets.token_hex(16)}.tmp"
        descriptor = -1
        try:
            descriptor = os.open(
                temporary,
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
            remaining = memoryview(resource.data)
            while remaining:
                written = os.write(descriptor, remaining[: 1024 * 1024])
                if written <= 0:
                    raise OSError
                remaining = remaining[written:]
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = -1
            os.replace(temporary, target)
            parent = os.open(target.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(parent)
            finally:
                os.close(parent)
        except OSError:
            with contextlib.suppress(OSError):
                temporary.unlink()
            raise LocalAgentClientError(LocalAgentClientErrorCode.UNAVAILABLE) from None
        finally:
            if descriptor >= 0:
                with contextlib.suppress(OSError):
                    os.close(descriptor)
        return {
            "schema_version": 1,
            "uri": resource.uri,
            "destination": str(target),
            "name": resource.name,
            "media_type": resource.media_type,
            "sha256": hashlib.sha256(resource.data).hexdigest(),
            "size_bytes": len(resource.data),
        }

    def close(self) -> None:
        with self._kernel_lock:
            if os.getpid() != self._pid:
                raise LocalAgentClientError(LocalAgentClientErrorCode.WRONG_PROCESS)
            if self._closed:
                return
            self._closed = True
            try:
                self._kernel.close()
            except BaseException:
                raise LocalAgentClientError(LocalAgentClientErrorCode.UNAVAILABLE) from None

    def __enter__(self) -> LocalAgentClient:
        self._ensure_live()
        return self

    def __exit__(self, _exc_type, _exc, _traceback) -> None:
        self.close()


__all__ = (
    "LocalAgentClient",
    "LocalAgentClientError",
    "LocalAgentClientErrorCode",
)
