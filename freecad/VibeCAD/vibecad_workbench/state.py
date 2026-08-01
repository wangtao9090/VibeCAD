import re
from dataclasses import dataclass

__all__ = (
    "ProjectionError",
    "ProjectSummary",
    "ProjectPage",
    "TaskSummary",
    "TaskPage",
    "project_page_from_mapping",
    "task_page_from_mapping",
)

_MAX_GENERATION = 9_007_199_254_740_991
_PROJECT_ID = re.compile(r"project_[0-9a-f]{32}")
_REVISION_ID = re.compile(r"revision_[0-9a-f]{32}")
_TASK_ID = re.compile(r"task_[0-9a-f]{32}")
_DRAFT_ID = re.compile(r"draft_[0-9a-f]{32}")
_DIGEST = re.compile(r"[0-9a-f]{64}")
_OUTER_KEYS = frozenset(("schema_version", "ok", "result", "error"))
_PROJECT_RESULT_KEYS = frozenset(("schema_version", "projects", "next_cursor"))
_PROJECT_KEYS = frozenset(
    (
        "schema_version",
        "project_id",
        "generation",
        "revision_id",
        "manifest_sha256",
    )
)
_TASK_RESULT_KEYS = frozenset(("tasks", "next_cursor"))
_TASK_KEYS = frozenset(
    (
        "task_id",
        "project_id",
        "generation",
        "base_revision",
        "reasoning_owner",
        "review_policy",
        "status",
        "next_action",
        "candidate_revision",
        "committed_revision",
        "draft_id",
    )
)


class ProjectionError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class ProjectSummary:
    project_id: str
    generation: int
    revision_id: str
    manifest_sha256: str


@dataclass(frozen=True, slots=True)
class ProjectPage:
    projects: tuple[ProjectSummary, ...]
    next_cursor: str | None


@dataclass(frozen=True, slots=True)
class TaskSummary:
    task_id: str
    project_id: str
    generation: int
    base_revision: str
    reasoning_owner: str
    review_policy: str
    status: str
    next_action: str
    candidate_revision: str | None
    committed_revision: str | None
    draft_id: str | None


@dataclass(frozen=True, slots=True)
class TaskPage:
    tasks: tuple[TaskSummary, ...]
    next_cursor: str | None


@dataclass(frozen=True, slots=True)
class PreviewProjection:
    head_open: bool
    draft_open: bool
    review_eligible: bool
    recovery_required: bool


def _preview_projection(
    *,
    head_open: bool,
    draft_open: bool,
    requested_eligible: bool,
    recovery_required: bool,
) -> PreviewProjection:
    head = head_open is True
    draft = draft_open is True
    recovery = recovery_required is True
    return PreviewProjection(
        head_open=head,
        draft_open=draft,
        review_eligible=requested_eligible is True and head and draft and not recovery,
        recovery_required=recovery,
    )


def _invalid() -> None:
    raise ProjectionError("invalid public mapping")


def _plain_dict(value: object, keys: frozenset[str]) -> dict[str, object]:
    if type(value) is not dict or any(type(key) is not str for key in value) or set(value) != keys:
        _invalid()
    return value


def _plain_list(value: object) -> list[object]:
    if type(value) is not list:
        _invalid()
    return value


def _schema_version(value: object) -> None:
    if type(value) is not int or value != 1:
        _invalid()


def _identifier(value: object, pattern: re.Pattern[str]) -> str:
    if type(value) is not str or pattern.fullmatch(value) is None:
        _invalid()
    return value


def _generation(value: object) -> int:
    if type(value) is not int or not 0 <= value <= _MAX_GENERATION:
        _invalid()
    return value


def _nonempty_string(value: object) -> str:
    if type(value) is not str or not value:
        _invalid()
    return value


def _optional_identifier(
    value: object,
    pattern: re.Pattern[str],
) -> str | None:
    if value is None:
        return None
    return _identifier(value, pattern)


def _cursor(value: object) -> str | None:
    if value is None:
        return None
    return _nonempty_string(value)


def _result_from_outer(mapping: object) -> object:
    outer = _plain_dict(mapping, _OUTER_KEYS)
    _schema_version(outer["schema_version"])
    if outer["ok"] is not True or outer["error"] is not None:
        _invalid()
    return outer["result"]


def project_page_from_mapping(mapping: object) -> ProjectPage:
    result = _plain_dict(_result_from_outer(mapping), _PROJECT_RESULT_KEYS)
    _schema_version(result["schema_version"])
    records = _plain_list(result["projects"])
    projects: list[ProjectSummary] = []
    previous_id: str | None = None
    for value in records:
        record = _plain_dict(value, _PROJECT_KEYS)
        _schema_version(record["schema_version"])
        project_id = _identifier(record["project_id"], _PROJECT_ID)
        if previous_id is not None and project_id <= previous_id:
            _invalid()
        projects.append(
            ProjectSummary(
                project_id=project_id,
                generation=_generation(record["generation"]),
                revision_id=_identifier(record["revision_id"], _REVISION_ID),
                manifest_sha256=_identifier(record["manifest_sha256"], _DIGEST),
            )
        )
        previous_id = project_id
    return ProjectPage(tuple(projects), _cursor(result["next_cursor"]))


def task_page_from_mapping(mapping: object) -> TaskPage:
    result = _plain_dict(_result_from_outer(mapping), _TASK_RESULT_KEYS)
    records = _plain_list(result["tasks"])
    tasks: list[TaskSummary] = []
    previous_id: str | None = None
    for value in records:
        record = _plain_dict(value, _TASK_KEYS)
        task_id = _identifier(record["task_id"], _TASK_ID)
        if previous_id is not None and task_id <= previous_id:
            _invalid()
        tasks.append(
            TaskSummary(
                task_id=task_id,
                project_id=_identifier(record["project_id"], _PROJECT_ID),
                generation=_generation(record["generation"]),
                base_revision=_identifier(record["base_revision"], _REVISION_ID),
                reasoning_owner=_nonempty_string(record["reasoning_owner"]),
                review_policy=_nonempty_string(record["review_policy"]),
                status=_nonempty_string(record["status"]),
                next_action=_nonempty_string(record["next_action"]),
                candidate_revision=_optional_identifier(
                    record["candidate_revision"],
                    _REVISION_ID,
                ),
                committed_revision=_optional_identifier(
                    record["committed_revision"],
                    _REVISION_ID,
                ),
                draft_id=_optional_identifier(record["draft_id"], _DRAFT_ID),
            )
        )
        previous_id = task_id
    return TaskPage(tuple(tasks), _cursor(result["next_cursor"]))
