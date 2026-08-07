"""Closed Task binding for one private freeform design creation.

The reserved operation is durable Task data, not a public model-program
operation.  Only the Task kernel may recognize it, and only while the project
still points at its generation-zero empty Revision.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from enum import StrEnum

from vibecad.execution.revisions import ProjectHead, RevisionRef
from vibecad.freeform.contracts import FreeformDesign
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
    task_creation_identity,
)

__all__ = (
    "BoundFreeformCreate",
    "FreeformCreateError",
    "FreeformCreateErrorCode",
    "build_freeform_create_binding",
    "parse_bound_freeform_create_task",
)

MAX_INTEGRATION_CONTROL_POINTS = 128
MAX_INTEGRATION_JSON_NODES = 768
_INTENT_DOMAIN = b"vibecad-freeform-create-intent-v1\0"
_RESERVED_OPERATION = "system.create_freeform_design"
_COMMAND_ID = "system-create-freeform-design"
_ACCEPTANCE_ID = "acceptance-system-create-freeform-design-v1"


class FreeformCreateErrorCode(StrEnum):
    INVALID_INPUT = "invalid_input"
    BUDGET_EXCEEDED = "budget_exceeded"


class FreeformCreateError(ValueError):
    __slots__ = ("code",)

    def __init__(self, code: FreeformCreateErrorCode) -> None:
        if type(code) is not FreeformCreateErrorCode:
            raise TypeError("code must be a FreeformCreateErrorCode")
        self.code = code
        super().__init__("The private freeform creation request is invalid.")


@dataclass(frozen=True, slots=True, kw_only=True)
class BoundFreeformCreate:
    create_key: str
    task_id: str
    creation_digest: str
    intent_digest: str
    project_id: str
    expected_head: ProjectHead
    empty_revision: RevisionRef
    design: FreeformDesign
    design_digest: str
    program: ModelProgram

    @property
    def reservation_key(self) -> str:
        return f"freeform:{self.intent_digest}"


def _fail(code: FreeformCreateErrorCode = FreeformCreateErrorCode.INVALID_INPUT) -> None:
    raise FreeformCreateError(code)


def _canonical(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("ascii")
    except Exception:
        _fail()


def _json_nodes(value: object) -> int:
    value_type = type(value)
    if value_type is dict:
        return 1 + sum(1 + _json_nodes(item) for item in value.values())
    if value_type is list:
        return 1 + sum(_json_nodes(item) for item in value)
    if value_type in {type(None), bool, int, float, str}:
        return 1
    _fail()


def _within_integration_budget(design: FreeformDesign) -> bool:
    try:
        control_points = sum(len(curve.control_points) for curve in design.curves)
        mapping = design.to_mapping()
        return (
            control_points <= MAX_INTEGRATION_CONTROL_POINTS
            and _json_nodes(mapping) <= MAX_INTEGRATION_JSON_NODES
        )
    except Exception:
        return False


def _empty_generation_zero_revision(
    project_id: str,
    expected_head: ProjectHead,
    revision: RevisionRef,
) -> bool:
    return (
        type(expected_head) is ProjectHead
        and expected_head.project_id == project_id
        and expected_head.generation == 0
        and type(revision) is RevisionRef
        and revision.id == expected_head.revision_id
        and revision.project_id == project_id
        and revision.manifest_sha256 == expected_head.manifest_sha256
        and revision.base_revision is None
        and revision.model is None
        and revision.artifacts == ()
    )


def _acceptance() -> AcceptanceSpec:
    return AcceptanceSpec(
        id=_ACCEPTANCE_ID,
        criteria=(
            AcceptanceCriterion(
                id="freeform-valid-shape",
                kind=AcceptanceKind.TOPOLOGY,
                check="valid_shape",
                target="body",
                expected=True,
            ),
            AcceptanceCriterion(
                id="freeform-single-solid",
                kind=AcceptanceKind.TOPOLOGY,
                check="solid_count",
                target="body",
                expected=1,
            ),
            AcceptanceCriterion(
                id="freeform-model-exists",
                kind=AcceptanceKind.ARTIFACT,
                check="exists",
                target="model",
                expected=True,
            ),
            AcceptanceCriterion(
                id="freeform-model-non-empty",
                kind=AcceptanceKind.ARTIFACT,
                check="non_empty",
                target="model",
                expected=True,
            ),
            AcceptanceCriterion(
                id="freeform-model-format",
                kind=AcceptanceKind.ARTIFACT,
                check="format",
                target="model",
                expected="fcstd",
            ),
            AcceptanceCriterion(
                id="freeform-export-exists",
                kind=AcceptanceKind.ARTIFACT,
                check="exists",
                target="export",
                expected=True,
            ),
            AcceptanceCriterion(
                id="freeform-export-non-empty",
                kind=AcceptanceKind.ARTIFACT,
                check="non_empty",
                target="export",
                expected=True,
            ),
            AcceptanceCriterion(
                id="freeform-export-format",
                kind=AcceptanceKind.ARTIFACT,
                check="format",
                target="export",
                expected="step",
            ),
        ),
    )


def build_freeform_create_binding(
    *,
    create_key: str,
    project_id: str,
    expected_head: ProjectHead,
    empty_revision: RevisionRef,
    design: FreeformDesign,
) -> BoundFreeformCreate:
    try:
        task_id, creation_digest = task_creation_identity(create_key)
    except Exception:
        _fail()
    if (
        type(project_id) is not str
        or type(design) is not FreeformDesign
        or not _empty_generation_zero_revision(project_id, expected_head, empty_revision)
    ):
        _fail()
    if not _within_integration_budget(design):
        _fail(FreeformCreateErrorCode.BUDGET_EXCEEDED)
    design_digest = design.digest
    body = {
        "schema_version": 1,
        "create_key": create_key,
        "project_id": project_id,
        "expected_head": expected_head.to_mapping(),
        "empty_revision": empty_revision.to_mapping(),
        "design": design.to_mapping(),
        "design_sha256": design_digest,
    }
    intent_digest = hashlib.sha256(_INTENT_DOMAIN + _canonical(body)).hexdigest()
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
                    "empty_revision": empty_revision.to_mapping(),
                    "intent_sha256": intent_digest,
                },
                args={
                    "create_key": create_key,
                    "design": design.to_mapping(),
                    "design_sha256": design_digest,
                },
                preserve=(),
                source=ValueSource.SYSTEM,
                depends_on=(),
            ),
        ),
        acceptance=_acceptance(),
    )
    return BoundFreeformCreate(
        create_key=create_key,
        task_id=task_id,
        creation_digest=creation_digest,
        intent_digest=intent_digest,
        project_id=project_id,
        expected_head=expected_head,
        empty_revision=empty_revision,
        design=design,
        design_digest=design_digest,
        program=program,
    )


def _task_value(task_or_stored: object) -> TaskRun:
    if type(task_or_stored) is TaskRun:
        return task_or_stored
    task = getattr(task_or_stored, "task_run", None)
    if type(task) is TaskRun:
        return task
    _fail()


def parse_bound_freeform_create_task(task_or_stored: object) -> BoundFreeformCreate | None:
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
        if set(operation.target) != {
            "project_id",
            "expected_head",
            "empty_revision",
            "intent_sha256",
        } or set(operation.args) != {"create_key", "design", "design_sha256"}:
            return None
        plain = program.to_mapping()["operations"][0]
        target = plain["target"]
        args = plain["args"]
        design = FreeformDesign.from_mapping(args["design"])
        binding = build_freeform_create_binding(
            create_key=args["create_key"],
            project_id=target["project_id"],
            expected_head=ProjectHead.from_mapping(target["expected_head"]),
            empty_revision=RevisionRef.from_mapping(target["empty_revision"]),
            design=design,
        )
    except Exception:
        return None
    if not (
        args["design_sha256"] == binding.design_digest
        and target["intent_sha256"] == binding.intent_digest
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
