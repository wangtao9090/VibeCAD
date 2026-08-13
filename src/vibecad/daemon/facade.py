"""Closed, code-installed facade for the local Task Kernel."""

from __future__ import annotations

import os
import re
from functools import partial

from vibecad import __version__
from vibecad.application.task_api import (
    TaskServicePortErrorCode,
    TaskServicePortFailure,
)
from vibecad.application.visual_ingress import (
    VisualIngressError,
    VisualIngressErrorCode,
    open_visual_staging,
    parse_seal_image_set_request,
    validate_seal_result,
)
from vibecad.interaction.checkouts import (
    CheckoutError,
    CheckoutErrorCode,
    DraftCheckoutSource,
    HeadCheckoutSource,
)
from vibecad.interaction.file_grants import (
    FileGrantBroker,
    FileGrantError,
    FileGrantErrorCode,
)
from vibecad.interaction.protocol_v2 import (
    V2_VERSION,
    StaticV2Dispatcher,
    V2ErrorCode,
    V2ProtocolError,
)
from vibecad.visual.inputs import VisualInputStoreError, VisualInputStoreErrorCode
from vibecad.workflow.store import StoredTaskRun

ALLOWED_APPLICATION_OPERATIONS = frozenset(
    {
        "accept_draft",
        "cancel_task",
        "compare_revisions",
        "create_release",
        "create_box",
        "create_cylinder",
        "create_project",
        "create_task",
        "export_task_artifacts",
        "get_artifact_manifest",
        "get_capabilities",
        "get_project",
        "get_release",
        "get_task",
        "get_task_events",
        "inspect_model",
        "list_projects",
        "list_revisions",
        "list_tasks",
        "modify_parameter",
        "move_part",
        "reject_draft",
        "resume_task",
        "revert_project",
        "rotate_part",
        "submit_model_program",
        "approve_release",
        "create_reconstruction",
        "get_reconstruction",
        "run_reconstruction",
        "answer_reconstruction",
        "adopt_reconstruction",
        "reject_reconstruction",
        "delete_reconstruction",
    }
)
KERNEL_API_EPOCH = 1
KERNEL_API_NAME = "vibecad.task-kernel"
KERNEL_BUILD_ID = "vcad-a12-native-part-primitives"

_DIRECT_OPERATIONS = frozenset(
    {
        "create_box",
        "create_cylinder",
        "inspect_model",
        "modify_parameter",
        "move_part",
        "rotate_part",
    }
)
_SESSION_RE = re.compile(r"session_[0-9a-f]{32}\Z")


def _checkout_failure(error: CheckoutError) -> V2ProtocolError:
    if error.code is CheckoutErrorCode.INVALID_INPUT:
        return V2ProtocolError(V2ErrorCode.INVALID_REQUEST)
    if error.code is CheckoutErrorCode.RESOURCE_EXHAUSTED:
        return V2ProtocolError(V2ErrorCode.RESOURCE_EXHAUSTED)
    return V2ProtocolError(V2ErrorCode.UNAVAILABLE)


def _file_grant_failure(error: FileGrantError) -> V2ProtocolError:
    if error.code is FileGrantErrorCode.INVALID_INPUT:
        return V2ProtocolError(V2ErrorCode.INVALID_REQUEST)
    if error.code is FileGrantErrorCode.RESOURCE_EXHAUSTED:
        return V2ProtocolError(V2ErrorCode.RESOURCE_EXHAUSTED)
    return V2ProtocolError(V2ErrorCode.UNAVAILABLE)


def _checkpoint_failure(error: TaskServicePortFailure) -> V2ProtocolError:
    if error.code is TaskServicePortErrorCode.INVALID_INPUT:
        return V2ProtocolError(V2ErrorCode.INVALID_REQUEST)
    if error.code is TaskServicePortErrorCode.RESOURCE_EXHAUSTED:
        return V2ProtocolError(V2ErrorCode.RESOURCE_EXHAUSTED)
    return V2ProtocolError(V2ErrorCode.UNAVAILABLE)


def _visual_ingress_failure(error: BaseException) -> V2ProtocolError:
    if type(error) is VisualIngressError:
        if error.code is VisualIngressErrorCode.INVALID_INPUT:
            return V2ProtocolError(V2ErrorCode.INVALID_REQUEST)
        if error.code is VisualIngressErrorCode.BUDGET_EXCEEDED:
            return V2ProtocolError(V2ErrorCode.RESOURCE_EXHAUSTED)
        return V2ProtocolError(V2ErrorCode.UNAVAILABLE)
    if type(error) is VisualInputStoreError:
        if error.code in {
            VisualInputStoreErrorCode.CONFLICT,
            VisualInputStoreErrorCode.INVALID_INPUT,
        }:
            return V2ProtocolError(V2ErrorCode.INVALID_REQUEST)
        if error.code is VisualInputStoreErrorCode.BUDGET_EXCEEDED:
            return V2ProtocolError(V2ErrorCode.RESOURCE_EXHAUSTED)
    return V2ProtocolError(V2ErrorCode.UNAVAILABLE)


class LocalKernelFacade:
    """Literal application routing; wire strings never select Python attributes."""

    __slots__ = ("_application", "_daemon_id", "_file_grants")

    def __init__(
        self,
        application: object,
        *,
        daemon_id: str,
    ) -> None:
        if application is None or type(daemon_id) is not str:
            raise TypeError("invalid local kernel facade")
        self._application = application
        self._daemon_id = daemon_id
        self._file_grants = FileGrantBroker(daemon_id)

    def open_session(self, session_id: object) -> StaticV2Dispatcher:
        if type(session_id) is not str or _SESSION_RE.fullmatch(session_id) is None:
            raise V2ProtocolError(V2ErrorCode.INVALID_STATE)
        return StaticV2Dispatcher(
            kernel_ping=self._kernel_ping,
            kernel_retire=self._kernel_retire,
            application_call=self._application_call,
            project_import=self._project_import,
            visual_inputs_seal=self._visual_inputs_seal,
            checkout_open=partial(self._checkout_open, session_id),
            checkout_get=self._checkout_get,
            checkout_checkpoint=self._checkout_checkpoint,
            checkout_close=self._checkout_close,
            file_grant_claim=partial(self._file_grant_claim, session_id),
            allowed_application_operations=ALLOWED_APPLICATION_OPERATIONS,
        )

    def close_session(self, session_id: object) -> None:
        try:
            self._file_grants.close_session(session_id)
        except FileGrantError as error:
            raise _file_grant_failure(error) from None

    def close(self) -> None:
        try:
            self._file_grants.close()
        except FileGrantError as error:
            raise _file_grant_failure(error) from None

    def _kernel_ping(self, _params: dict[str, object]) -> dict[str, object]:
        return {
            "schema_version": 1,
            "daemon_id": self._daemon_id,
            "status": "ready",
            "protocol": {"major": V2_VERSION[0], "minor": V2_VERSION[1]},
            "api": {
                "name": KERNEL_API_NAME,
                "epoch": KERNEL_API_EPOCH,
            },
            "implementation": {
                "package_version": __version__,
                "build_id": KERNEL_BUILD_ID,
            },
        }

    def _kernel_retire(self, params: dict[str, object]) -> dict[str, object]:
        if params["daemon_id"] != self._daemon_id:
            raise V2ProtocolError(V2ErrorCode.INVALID_REQUEST)
        return {
            "schema_version": 1,
            "daemon_id": self._daemon_id,
            "status": "retiring",
        }

    def _application_call(self, params: dict[str, object]) -> dict[str, object]:
        operation = params["operation"]
        request = params["request"]
        if operation == "create_project":
            if request.get("kind") == "import_fcstd" or request.get("source_path") is not None:
                raise V2ProtocolError(V2ErrorCode.UNAVAILABLE)
            return self._application.create_project_request(request)
        if operation == "get_project":
            return self._application.get_project_request(request)
        if operation == "list_projects":
            return self._application.list_projects_request(request)
        if operation == "list_revisions":
            return self._application.list_revisions_request(request)
        if operation == "compare_revisions":
            return self._application.compare_revisions_request(request)
        if operation == "revert_project":
            return self._application.revert_project_request(request)
        if operation == "create_task":
            return self._application.create_task_request(request)
        if operation == "list_tasks":
            return self._application.list_tasks_request(request)
        if operation == "get_task":
            return self._application.get_task_request(request)
        if operation == "get_task_events":
            return self._application.get_task_events_request(request)
        if operation == "submit_model_program":
            return self._application.submit_model_program_request(request)
        if operation == "resume_task":
            return self._application.resume_task_request(request)
        if operation == "cancel_task":
            return self._application.cancel_task_request(request)
        if operation == "accept_draft":
            return self._application.accept_draft_request(request)
        if operation == "reject_draft":
            return self._application.reject_draft_request(request)
        if operation == "get_artifact_manifest":
            return self._application.get_artifact_manifest_request(request)
        if operation == "export_task_artifacts":
            return self._application.export_task_artifacts_request(request)
        if operation == "create_release":
            return self._application.create_release_request(request)
        if operation == "get_release":
            return self._application.get_release_request(request)
        if operation == "approve_release":
            return self._application.approve_release_request(request)
        if operation == "create_reconstruction":
            return self._application.create_reconstruction_request(request)
        if operation == "get_reconstruction":
            return self._application.get_reconstruction_request(request)
        if operation == "run_reconstruction":
            return self._application.run_reconstruction_request(request)
        if operation == "answer_reconstruction":
            return self._application.answer_reconstruction_request(request)
        if operation == "adopt_reconstruction":
            return self._application.adopt_reconstruction_request(request)
        if operation == "reject_reconstruction":
            return self._application.reject_reconstruction_request(request)
        if operation == "delete_reconstruction":
            return self._application.delete_reconstruction_request(request)
        if operation == "get_capabilities":
            return self._application.get_capabilities_request(request)
        if operation in _DIRECT_OPERATIONS:
            return self._application.invoke_direct_operation_request(operation, request)
        raise V2ProtocolError(V2ErrorCode.UNKNOWN_METHOD)

    @staticmethod
    def _descriptor_matches(
        descriptor: int,
        locator: dict[str, object],
    ) -> bool:
        try:
            value = os.fstat(descriptor)
        except OSError:
            return False
        return (
            value.st_dev == locator["dev"]
            and value.st_ino == locator["ino"]
            and value.st_mode == locator["mode"]
            and value.st_uid == locator["uid"]
            and value.st_nlink == locator["nlink"]
            and value.st_size == locator["size"]
            and str(value.st_mtime_ns) == locator["mtime_ns"]
            and str(value.st_ctime_ns) == locator["ctime_ns"]
        )

    def _project_import(
        self,
        params: dict[str, object],
        descriptor: int,
    ) -> dict[str, object]:
        request = params["request"]
        locator = params["locator"]
        if (
            type(request) is not dict
            or type(locator) is not dict
            or not self._descriptor_matches(descriptor, locator)
        ):
            raise V2ProtocolError(V2ErrorCode.INVALID_REQUEST)
        result = self._application.import_project_descriptor_request(
            request,
            source_fd=descriptor,
            locator=locator,
        )
        return result

    def _visual_inputs_seal(
        self,
        params: dict[str, object],
        descriptor: int,
    ) -> dict[str, object]:
        try:
            request = parse_seal_image_set_request(params["request"])
            opened = open_visual_staging(request, descriptor, params["locator"])
            try:
                sealed = self._application.seal_visual_image_set(
                    request=request,
                    sources=opened.sources,
                )
            finally:
                opened.close()
        except (KeyError, VisualIngressError, VisualInputStoreError) as error:
            raise _visual_ingress_failure(error) from None
        return validate_seal_result(
            {
                "schema_version": 1,
                "image_set_id": getattr(sealed, "id", None),
                "image_set_manifest_sha256": getattr(sealed, "manifest_sha256", None),
            }
        )

    @staticmethod
    def _source(value: dict[str, object]) -> HeadCheckoutSource | DraftCheckoutSource:
        try:
            if value["kind"] == "head":
                return HeadCheckoutSource(project_id=value["project_id"])
            return DraftCheckoutSource(
                task_id=value["task_id"],
                draft_id=value["draft_id"],
                expected_generation=value["expected_generation"],
            )
        except (CheckoutError, KeyError, TypeError, ValueError):
            raise V2ProtocolError(V2ErrorCode.INVALID_REQUEST) from None

    @staticmethod
    def _descriptor(value: object) -> dict[str, object]:
        try:
            result = value.to_local_mapping()
        except (AttributeError, CheckoutError, TypeError, ValueError):
            raise V2ProtocolError(V2ErrorCode.INTERNAL_ERROR) from None
        if type(result) is not dict:
            raise V2ProtocolError(V2ErrorCode.INTERNAL_ERROR)
        return result

    def _checkout_open(
        self,
        session_id: str,
        params: dict[str, object],
    ) -> dict[str, object]:
        try:
            descriptor = self._application.open_checkout(
                open_key=params["open_key"],
                source=self._source(params["source"]),
            )
            snapshot, grant = self._file_grants.mint(
                session_id=session_id,
                checkout_id=descriptor.checkout_id,
                capture=partial(
                    self._application.capture_checkout_file,
                    checkout_id=descriptor.checkout_id,
                ),
            )
        except CheckoutError as error:
            raise _checkout_failure(error) from None
        except FileGrantError as error:
            raise _file_grant_failure(error) from None
        return self._descriptor(snapshot.descriptor) | {
            "file_grant": grant.to_mapping(),
        }

    def _checkout_get(self, params: dict[str, object]) -> dict[str, object]:
        try:
            descriptor = self._application.get_checkout(
                checkout_id=params["checkout_id"],
            )
        except CheckoutError as error:
            raise _checkout_failure(error) from None
        return self._descriptor(descriptor)

    def _checkout_checkpoint(self, params: dict[str, object]) -> dict[str, object]:
        try:
            result = self._application.checkpoint_checkout(
                checkpoint_key=params["checkpoint_key"],
                checkout_id=params["checkout_id"],
            )
        except CheckoutError as error:
            raise _checkout_failure(error) from None
        if type(result) is TaskServicePortFailure:
            raise _checkpoint_failure(result)
        if type(result) is not StoredTaskRun:
            raise V2ProtocolError(V2ErrorCode.INTERNAL_ERROR)
        return {
            "schema_version": 1,
            "generation": result.generation,
            "next_action": result.task_run.next_action.value,
            "task_run": result.task_run.to_mapping(),
        }

    def _checkout_close(self, params: dict[str, object]) -> dict[str, object]:
        checkout_id = params["checkout_id"]
        try:
            self._file_grants.revoke_checkout(checkout_id)
            try:
                descriptor = self._application.close_checkout(
                    checkout_id=checkout_id,
                )
            finally:
                self._file_grants.revoke_checkout(checkout_id)
        except CheckoutError as error:
            raise _checkout_failure(error) from None
        except FileGrantError as error:
            raise _file_grant_failure(error) from None
        return self._descriptor(descriptor)

    def _file_grant_claim(
        self,
        session_id: str,
        params: dict[str, object],
    ) -> dict[str, object]:
        try:
            claim = self._file_grants.claim(
                session_id=session_id,
                grant_id=params["grant_id"],
                require_same=self._application.require_same_checkout_file,
            )
        except CheckoutError as error:
            raise _checkout_failure(error) from None
        except FileGrantError as error:
            raise _file_grant_failure(error) from None
        return claim.to_mapping()


__all__ = ("ALLOWED_APPLICATION_OPERATIONS", "LocalKernelFacade")
