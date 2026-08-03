"""Closed durable contract for publishing one user-edited managed checkout.

The reserved operation is data carried by a TaskRun, not an operation exposed
to the ordinary model-program executor.  TaskService may recognize it only
after recomputing every checkout, content, base-HEAD, and policy binding.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from enum import StrEnum

from vibecad.execution.revisions import ProjectHead
from vibecad.workflow.contracts import (
    AcceptanceCriterion,
    AcceptanceKind,
    AcceptanceSpec,
    ModelCommand,
    ModelProgram,
    ValueSource,
)
from vibecad.workflow.state import ReasoningOwner, ReviewPolicy, TaskRun

__all__ = (
    "BoundManualCheckpoint",
    "ManualCheckpointError",
    "ManualCheckpointErrorCode",
    "build_manual_checkpoint_binding",
    "manual_checkpoint_task_identity",
    "parse_bound_manual_checkpoint_task",
    "require_matching_manual_checkpoint_task",
)

_CHECKPOINT_KEY_PATTERN = re.compile(r"checkpoint_create_[0-9a-f]{32}\Z")
_CHECKOUT_PATTERN = re.compile(r"checkout_[0-9a-f]{32}\Z")
_PROJECT_PATTERN = re.compile(r"project_[0-9a-f]{32}\Z")
_DIGEST_PATTERN = re.compile(r"[0-9a-f]{64}\Z")
_MAX_FILE_BYTES = 536_870_912
_CREATION_DOMAIN = b"vibecad-manual-checkpoint-create-v1\0"
_INTENT_DOMAIN = b"vibecad-manual-checkpoint-intent-v1\0"
_RESERVED_OPERATION = "system.checkpoint_checkout"
_COMMAND_ID = "user-checkpoint-managed-checkout"
_ACCEPTANCE_ID = "acceptance-user-checkpoint-single-part-v1"


class ManualCheckpointErrorCode(StrEnum):
    INVALID_INPUT = "invalid_input"
    CONFLICT = "conflict"


class ManualCheckpointError(ValueError):
    __slots__ = ("code",)

    def __init__(self, code: ManualCheckpointErrorCode) -> None:
        if type(code) is not ManualCheckpointErrorCode:
            raise TypeError("code must be a ManualCheckpointErrorCode")
        self.code = code
        super().__init__(
            "The manual checkpoint input is invalid."
            if code is ManualCheckpointErrorCode.INVALID_INPUT
            else "The checkpoint key is already bound to different immutable intent."
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class BoundManualCheckpoint:
    checkpoint_key: str
    checkout_id: str
    task_id: str
    creation_digest: str
    intent_digest: str
    project_id: str
    expected_head: ProjectHead
    model_sha256: str
    model_size_bytes: int
    program: ModelProgram

    @property
    def reservation_key(self) -> str:
        return f"checkpoint:{self.intent_digest}"


def _invalid() -> None:
    raise ManualCheckpointError(ManualCheckpointErrorCode.INVALID_INPUT)


def _conflict() -> None:
    raise ManualCheckpointError(ManualCheckpointErrorCode.CONFLICT)


def manual_checkpoint_task_identity(checkpoint_key: str) -> tuple[str, str]:
    if type(checkpoint_key) is not str or _CHECKPOINT_KEY_PATTERN.fullmatch(checkpoint_key) is None:
        _invalid()
    digest = hashlib.sha256(_CREATION_DOMAIN + checkpoint_key.encode("ascii")).hexdigest()
    return (f"task_{digest[:32]}", digest)


def _canonical_json(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=True,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("ascii")
    except Exception:
        _invalid()


def _acceptance_spec() -> AcceptanceSpec:
    return AcceptanceSpec(
        id=_ACCEPTANCE_ID,
        criteria=(
            AcceptanceCriterion(
                id="checkpoint-valid-shape",
                kind=AcceptanceKind.TOPOLOGY,
                check="valid_shape",
                target="body",
                expected=True,
            ),
            AcceptanceCriterion(
                id="checkpoint-single-solid",
                kind=AcceptanceKind.TOPOLOGY,
                check="solid_count",
                target="body",
                expected=1,
            ),
            AcceptanceCriterion(
                id="checkpoint-model-exists",
                kind=AcceptanceKind.ARTIFACT,
                check="exists",
                target="model",
                expected=True,
            ),
            AcceptanceCriterion(
                id="checkpoint-model-non-empty",
                kind=AcceptanceKind.ARTIFACT,
                check="non_empty",
                target="model",
                expected=True,
            ),
            AcceptanceCriterion(
                id="checkpoint-model-format",
                kind=AcceptanceKind.ARTIFACT,
                check="format",
                target="model",
                expected="fcstd",
            ),
            AcceptanceCriterion(
                id="checkpoint-export-exists",
                kind=AcceptanceKind.ARTIFACT,
                check="exists",
                target="export",
                expected=True,
            ),
            AcceptanceCriterion(
                id="checkpoint-export-non-empty",
                kind=AcceptanceKind.ARTIFACT,
                check="non_empty",
                target="export",
                expected=True,
            ),
            AcceptanceCriterion(
                id="checkpoint-export-format",
                kind=AcceptanceKind.ARTIFACT,
                check="format",
                target="export",
                expected="step",
            ),
        ),
    )


def build_manual_checkpoint_binding(
    *,
    checkpoint_key: str,
    checkout_id: str,
    project_id: str,
    expected_head: ProjectHead,
    model_sha256: str,
    model_size_bytes: int,
) -> BoundManualCheckpoint:
    task_id, creation_digest = manual_checkpoint_task_identity(checkpoint_key)
    if not (
        type(checkout_id) is str
        and _CHECKOUT_PATTERN.fullmatch(checkout_id) is not None
        and type(project_id) is str
        and _PROJECT_PATTERN.fullmatch(project_id) is not None
        and type(expected_head) is ProjectHead
        and expected_head.project_id == project_id
        and type(model_sha256) is str
        and _DIGEST_PATTERN.fullmatch(model_sha256) is not None
        and type(model_size_bytes) is int
        and 0 < model_size_bytes <= _MAX_FILE_BYTES
    ):
        _invalid()
    intent_body = {
        "schema_version": 1,
        "checkpoint_key": checkpoint_key,
        "checkout_id": checkout_id,
        "project_id": project_id,
        "expected_head": expected_head.to_mapping(),
        "model_sha256": model_sha256,
        "model_size_bytes": model_size_bytes,
    }
    intent_digest = hashlib.sha256(_INTENT_DOMAIN + _canonical_json(intent_body)).hexdigest()
    program = ModelProgram(
        task_id=task_id,
        base_revision=expected_head.revision_id,
        operations=(
            ModelCommand(
                id=_COMMAND_ID,
                op=_RESERVED_OPERATION,
                target={
                    "project_id": project_id,
                    "expected_head": expected_head.to_mapping(),
                    "intent_sha256": intent_digest,
                },
                args={
                    "checkpoint_key": checkpoint_key,
                    "checkout_id": checkout_id,
                    "model_sha256": model_sha256,
                    "model_size_bytes": model_size_bytes,
                },
                preserve=(),
                source=ValueSource.USER,
                depends_on=(),
            ),
        ),
        acceptance=_acceptance_spec(),
    )
    return BoundManualCheckpoint(
        checkpoint_key=checkpoint_key,
        checkout_id=checkout_id,
        task_id=task_id,
        creation_digest=creation_digest,
        intent_digest=intent_digest,
        project_id=project_id,
        expected_head=expected_head,
        model_sha256=model_sha256,
        model_size_bytes=model_size_bytes,
        program=program,
    )


def _task_value(task_or_stored: object) -> TaskRun:
    if type(task_or_stored) is TaskRun:
        return task_or_stored
    task = getattr(task_or_stored, "task_run", None)
    if type(task) is TaskRun:
        return task
    _invalid()


def parse_bound_manual_checkpoint_task(
    task_or_stored: object,
) -> BoundManualCheckpoint | None:
    task = _task_value(task_or_stored)
    program = task.program
    if type(program) is not ModelProgram or len(program.operations) != 1:
        return None
    operation = program.operations[0]
    if not (
        type(operation) is ModelCommand
        and operation.op == _RESERVED_OPERATION
        and operation.source is ValueSource.USER
    ):
        return None
    try:
        target = operation.target
        args = operation.args
        if set(target) != {"project_id", "expected_head", "intent_sha256"} or set(args) != {
            "checkpoint_key",
            "checkout_id",
            "model_sha256",
            "model_size_bytes",
        }:
            return None
        mapping = program.to_mapping()["operations"][0]
        plain_target = mapping["target"]
        plain_args = mapping["args"]
        binding = build_manual_checkpoint_binding(
            checkpoint_key=plain_args["checkpoint_key"],
            checkout_id=plain_args["checkout_id"],
            project_id=plain_target["project_id"],
            expected_head=ProjectHead.from_mapping(plain_target["expected_head"]),
            model_sha256=plain_args["model_sha256"],
            model_size_bytes=plain_args["model_size_bytes"],
        )
    except Exception:
        return None
    if not (
        target["intent_sha256"] == binding.intent_digest
        and program == binding.program
        and task.id == binding.task_id
        and task.creation_digest == binding.creation_digest
        and task.project_id == binding.project_id
        and task.base_revision == binding.expected_head.revision_id
        and task.reasoning_owner is ReasoningOwner.EXTERNAL_PLAN
        and task.review_policy is ReviewPolicy.AUTO_COMMIT
    ):
        return None
    return binding


def require_matching_manual_checkpoint_task(
    task_or_stored: object,
    *,
    checkpoint_key: str,
    checkout_id: str,
    project_id: str,
    expected_head: str,
    model_sha256: str,
    model_size_bytes: int,
) -> BoundManualCheckpoint:
    manual_checkpoint_task_identity(checkpoint_key)
    binding = parse_bound_manual_checkpoint_task(task_or_stored)
    if binding is None:
        _conflict()
    if not (
        binding.checkpoint_key == checkpoint_key
        and binding.checkout_id == checkout_id
        and binding.project_id == project_id
        and binding.expected_head.revision_id == expected_head
        and binding.model_sha256 == model_sha256
        and binding.model_size_bytes == model_size_bytes
    ):
        _conflict()
    return binding
