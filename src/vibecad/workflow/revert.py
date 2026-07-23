"""Pure, closed contracts for one system-bound forward revert.

The reserved program in this module is durable data, not a generally
executable CAD operation.  Only :class:`~vibecad.workflow.service.TaskService`
may recognize it after every identity, intent, provenance, and payload binding
has been recomputed.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from enum import StrEnum

from vibecad.execution.revisions import ProjectHead, RevisionArtifactRef, RevisionRef
from vibecad.workflow.contracts import (
    AcceptanceCriterion,
    AcceptanceKind,
    AcceptanceSpec,
    ModelCommand,
    ModelProgram,
    ValueSource,
)
from vibecad.workflow.state import (
    ReasoningOwner,
    ReviewPolicy,
    TaskRun,
)

__all__ = (
    "BoundRevert",
    "RevertProgramError",
    "RevertProgramErrorCode",
    "build_revert_binding",
    "parse_bound_revert_task",
    "require_matching_revert_task",
    "revert_payload_matches_source",
    "revert_task_identity",
)

_REVERT_KEY_PATTERN = re.compile(r"revert_create_[0-9a-f]{32}\Z")
_CREATION_DOMAIN = b"vibecad-revert-create-v1\0"
_INTENT_DOMAIN = b"vibecad-revert-intent-v1\0"
_RESERVED_OPERATION = "system.restore_revision"
_COMMAND_ID = "system-restore-revision"
_ACCEPTANCE_ID = "acceptance-system-restore-revision-v1"


class RevertProgramErrorCode(StrEnum):
    """Closed pure-contract failures."""

    INVALID_INPUT = "invalid_input"
    CONFLICT = "conflict"


class RevertProgramError(ValueError):
    """A non-reflective failure for malformed or mismatched revert intent."""

    __slots__ = ("code",)

    def __init__(self, code: RevertProgramErrorCode) -> None:
        if type(code) is not RevertProgramErrorCode:
            raise TypeError("code must be a RevertProgramErrorCode")
        self.code = code
        super().__init__(
            "The revert input is invalid."
            if code is RevertProgramErrorCode.INVALID_INPUT
            else "The revert key is already bound to different immutable intent."
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class BoundRevert:
    """The fully recomputed authority carried by one reserved task program."""

    revert_key: str
    task_id: str
    creation_digest: str
    intent_digest: str
    project_id: str
    source_revision: RevisionRef
    expected_head: ProjectHead
    program: ModelProgram

    @property
    def reservation_key(self) -> str:
        return f"revert:{self.intent_digest}"


def _invalid() -> None:
    raise RevertProgramError(RevertProgramErrorCode.INVALID_INPUT)


def _conflict() -> None:
    raise RevertProgramError(RevertProgramErrorCode.CONFLICT)


def revert_task_identity(revert_key: str) -> tuple[str, str]:
    """Return the deterministic task id and creation digest for one public key."""

    if type(revert_key) is not str or _REVERT_KEY_PATTERN.fullmatch(revert_key) is None:
        _invalid()
    digest = hashlib.sha256(_CREATION_DOMAIN + revert_key.encode("ascii")).hexdigest()
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


def _intent_digest(
    *,
    revert_key: str,
    project_id: str,
    source_revision: RevisionRef,
    expected_head: ProjectHead,
) -> str:
    body = {
        "schema_version": 1,
        "revert_key": revert_key,
        "project_id": project_id,
        "source_revision": source_revision.to_mapping(),
        "expected_head": expected_head.to_mapping(),
    }
    return hashlib.sha256(_INTENT_DOMAIN + _canonical_json(body)).hexdigest()


def _acceptance_spec() -> AcceptanceSpec:
    return AcceptanceSpec(
        id=_ACCEPTANCE_ID,
        criteria=(
            AcceptanceCriterion(
                id="revert-valid-shape",
                kind=AcceptanceKind.TOPOLOGY,
                check="valid_shape",
                target="body",
                expected=True,
            ),
            AcceptanceCriterion(
                id="revert-model-exists",
                kind=AcceptanceKind.ARTIFACT,
                check="exists",
                target="model",
                expected=True,
            ),
            AcceptanceCriterion(
                id="revert-model-non-empty",
                kind=AcceptanceKind.ARTIFACT,
                check="non_empty",
                target="model",
                expected=True,
            ),
            AcceptanceCriterion(
                id="revert-model-format",
                kind=AcceptanceKind.ARTIFACT,
                check="format",
                target="model",
                expected="fcstd",
            ),
            AcceptanceCriterion(
                id="revert-export-exists",
                kind=AcceptanceKind.ARTIFACT,
                check="exists",
                target="export",
                expected=True,
            ),
            AcceptanceCriterion(
                id="revert-export-non-empty",
                kind=AcceptanceKind.ARTIFACT,
                check="non_empty",
                target="export",
                expected=True,
            ),
            AcceptanceCriterion(
                id="revert-export-format",
                kind=AcceptanceKind.ARTIFACT,
                check="format",
                target="export",
                expected="step",
            ),
        ),
    )


def _complete_source(source_revision: RevisionRef) -> bool:
    return (
        type(source_revision) is RevisionRef
        and source_revision.base_revision is not None
        and type(source_revision.model) is RevisionArtifactRef
        and source_revision.model.name == "model.FCStd"
        and source_revision.model.format == "fcstd"
        and type(source_revision.artifacts) is tuple
        and len(source_revision.artifacts) == 1
        and type(source_revision.artifacts[0]) is RevisionArtifactRef
        and source_revision.artifacts[0].name == "model.step"
        and source_revision.artifacts[0].format == "step"
    )


def build_revert_binding(
    *,
    revert_key: str,
    project_id: str,
    source_revision: RevisionRef,
    expected_head: ProjectHead,
) -> BoundRevert:
    """Build the only canonical reserved program accepted by the kernel."""

    task_id, creation_digest = revert_task_identity(revert_key)
    if not (
        type(project_id) is str
        and type(expected_head) is ProjectHead
        and expected_head.project_id == project_id
        and _complete_source(source_revision)
        and source_revision.project_id == project_id
        and source_revision.id != expected_head.revision_id
    ):
        _invalid()
    intent_digest = _intent_digest(
        revert_key=revert_key,
        project_id=project_id,
        source_revision=source_revision,
        expected_head=expected_head,
    )
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
                    "revert_key": revert_key,
                    "source_revision": source_revision.to_mapping(),
                },
                preserve=(),
                source=ValueSource.SYSTEM,
                depends_on=(),
            ),
        ),
        acceptance=_acceptance_spec(),
    )
    return BoundRevert(
        revert_key=revert_key,
        task_id=task_id,
        creation_digest=creation_digest,
        intent_digest=intent_digest,
        project_id=project_id,
        source_revision=source_revision,
        expected_head=expected_head,
        program=program,
    )


def _task_value(task_or_stored: object) -> TaskRun:
    if type(task_or_stored) is TaskRun:
        return task_or_stored
    task = getattr(task_or_stored, "task_run", None)
    if type(task) is TaskRun:
        return task
    _invalid()


def parse_bound_revert_task(task_or_stored: object) -> BoundRevert | None:
    """Recognize a bound revert only after its entire capability is recomputed."""

    task = _task_value(task_or_stored)
    program = task.program
    if type(program) is not ModelProgram or len(program.operations) != 1:
        return None
    operation = program.operations[0]
    if not (
        type(operation) is ModelCommand
        and operation.op == _RESERVED_OPERATION
        and operation.source is ValueSource.SYSTEM
    ):
        return None
    try:
        target = operation.target
        args = operation.args
        if set(target) != {"project_id", "expected_head", "intent_sha256"} or set(args) != {
            "revert_key",
            "source_revision",
        }:
            return None
        operation_mapping = program.to_mapping()["operations"][0]
        plain_target = operation_mapping["target"]
        plain_args = operation_mapping["args"]
        revert_key = args["revert_key"]
        project_id = target["project_id"]
        source_revision = RevisionRef.from_mapping(plain_args["source_revision"])
        expected_head = ProjectHead.from_mapping(plain_target["expected_head"])
        if type(revert_key) is not str or type(project_id) is not str:
            return None
        binding = build_revert_binding(
            revert_key=revert_key,
            project_id=project_id,
            source_revision=source_revision,
            expected_head=expected_head,
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
        and task.review_policy is ReviewPolicy.REQUIRE_REVIEW
    ):
        return None
    return binding


def require_matching_revert_task(
    task_or_stored: object,
    *,
    revert_key: str,
    project_id: str,
    source_revision: str,
    expected_head: str,
) -> BoundRevert:
    """Require one existing task to match the exact public immutable intent."""

    revert_task_identity(revert_key)
    if not all(type(value) is str for value in (project_id, source_revision, expected_head)):
        _invalid()
    binding = parse_bound_revert_task(task_or_stored)
    if binding is None:
        _conflict()
    if not (
        binding.revert_key == revert_key
        and binding.project_id == project_id
        and binding.source_revision.id == source_revision
        and binding.expected_head.revision_id == expected_head
    ):
        _conflict()
    return binding


def revert_payload_matches_source(
    candidate: RevisionRef,
    source: RevisionRef,
    *,
    expected_head: ProjectHead,
) -> bool:
    """Compare only the immutable payload facts a forward restore must preserve."""

    if not (
        type(candidate) is RevisionRef
        and type(source) is RevisionRef
        and type(expected_head) is ProjectHead
        and candidate.project_id == source.project_id == expected_head.project_id
        and candidate.base_revision == expected_head.revision_id
        and _complete_source(candidate)
        and _complete_source(source)
    ):
        return False
    assert candidate.model is not None
    assert source.model is not None
    candidate_step = candidate.artifacts[0]
    source_step = source.artifacts[0]
    return (
        candidate.model.name == source.model.name
        and candidate.model.format == source.model.format
        and candidate.model.sha256 == source.model.sha256
        and candidate.model.size_bytes == source.model.size_bytes
        and candidate_step.name == source_step.name
        and candidate_step.format == source_step.format
        and candidate_step.sha256 == source_step.sha256
        and candidate_step.size_bytes == source_step.size_bytes
    )
