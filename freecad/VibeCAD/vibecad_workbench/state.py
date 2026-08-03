import re
from dataclasses import dataclass

__all__ = (
    "ProjectionError",
    "ProjectSummary",
    "ProjectPage",
    "TaskSummary",
    "TaskPage",
    "ReleaseFileSummary",
    "ReleaseSummary",
    "project_page_from_mapping",
    "project_summary_from_detail_mapping",
    "task_summary_from_detail_mapping",
    "task_page_from_mapping",
    "release_summary_from_mapping",
)

_MAX_GENERATION = 9_007_199_254_740_991
_PROJECT_ID = re.compile(r"project_[0-9a-f]{32}")
_REVISION_ID = re.compile(r"revision_[0-9a-f]{32}")
_TASK_ID = re.compile(r"task_[0-9a-f]{32}")
_DRAFT_ID = re.compile(r"draft_[0-9a-f]{32}")
_DIGEST = re.compile(r"[0-9a-f]{64}")
_VERIFICATION_ID = re.compile(r"verification_[0-9a-f]{32}")
_ARTIFACT_ID = re.compile(r"artifact_[0-9a-f]{32}")
_RELEASE_ID = re.compile(r"release_[0-9a-f]{32}")
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
_PROJECT_DETAIL_RESULT_KEYS = frozenset(("schema_version", "project_id", "current"))
_PROJECT_CURRENT_KEYS = frozenset(("head", "revision"))
_REVISION_KEYS = frozenset(
    (
        "schema_version",
        "id",
        "project_id",
        "base_revision",
        "manifest_sha256",
        "model",
        "artifacts",
    )
)
_ARTIFACT_KEYS = frozenset(("schema_version", "id", "name", "format", "sha256", "size_bytes"))
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
_TASK_DETAIL_RESULT_KEYS = frozenset(("generation", "next_action", "task_run"))
_TASK_RUN_KEYS = frozenset(
    (
        "schema_version",
        "id",
        "project_id",
        "base_revision",
        "reasoning_owner",
        "review_policy",
        "status",
        "creation_digest",
        "program",
        "candidate_revision",
        "committed_revision",
        "draft",
        "steps",
        "verification_reports",
        "artifacts",
        "last_error",
        "transitions",
    )
)
_DRAFT_KEYS = frozenset(
    (
        "schema_version",
        "id",
        "task_id",
        "project_id",
        "base_revision",
        "base_generation",
        "base_manifest_sha256",
        "revision_id",
        "manifest_sha256",
        "verification_id",
        "acceptance_id",
        "observation_digest",
    )
)
_RELEASE_KEYS = frozenset(
    (
        "release_id",
        "status",
        "generation",
        "task_id",
        "task_generation",
        "project_id",
        "revision_id",
        "revision_manifest_sha256",
        "verification_id",
        "verification_digest",
        "observation_digest",
        "manifest",
        "drawing",
        "bom_json",
        "bom_csv",
        "validation_report_uri",
        "package",
        "approved_at_ms",
    )
)
_RELEASE_FILE_KEYS = frozenset(("name", "media_type", "sha256", "size_bytes", "resource_uri"))
_RELEASE_MEDIA_TYPES = {
    "manifest.json": "application/json",
    "assembly-drawing.pdf": "application/pdf",
    "bom.json": "application/json",
    "bom.csv": "text/csv",
    "vibecad-release.zip": "application/zip",
}


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
class ReleaseFileSummary:
    name: str
    media_type: str
    sha256: str
    size_bytes: int
    resource_uri: str | None


@dataclass(frozen=True, slots=True)
class ReleaseSummary:
    release_id: str
    status: str
    generation: int
    task_id: str
    task_generation: int
    project_id: str
    revision_id: str
    revision_manifest_sha256: str
    verification_id: str
    verification_digest: str
    observation_digest: str
    manifest: ReleaseFileSummary
    drawing: ReleaseFileSummary
    bom_json: ReleaseFileSummary
    bom_csv: ReleaseFileSummary
    validation_report_uri: str
    package: ReleaseFileSummary
    approved_at_ms: int | None


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


def _artifact(value: object) -> None:
    artifact = _plain_dict(value, _ARTIFACT_KEYS)
    _schema_version(artifact["schema_version"])
    _identifier(artifact["id"], _ARTIFACT_ID)
    _nonempty_string(artifact["name"])
    _nonempty_string(artifact["format"])
    _identifier(artifact["sha256"], _DIGEST)
    _generation(artifact["size_bytes"])


def project_summary_from_detail_mapping(mapping: object) -> ProjectSummary:
    result = _plain_dict(_result_from_outer(mapping), _PROJECT_DETAIL_RESULT_KEYS)
    _schema_version(result["schema_version"])
    project_id = _identifier(result["project_id"], _PROJECT_ID)
    current = _plain_dict(result["current"], _PROJECT_CURRENT_KEYS)
    head = _plain_dict(current["head"], _PROJECT_KEYS)
    revision = _plain_dict(current["revision"], _REVISION_KEYS)
    _schema_version(head["schema_version"])
    _schema_version(revision["schema_version"])
    revision_id = _identifier(head["revision_id"], _REVISION_ID)
    manifest = _identifier(head["manifest_sha256"], _DIGEST)
    if (
        _identifier(head["project_id"], _PROJECT_ID) != project_id
        or _identifier(revision["id"], _REVISION_ID) != revision_id
        or _identifier(revision["project_id"], _PROJECT_ID) != project_id
        or _identifier(revision["manifest_sha256"], _DIGEST) != manifest
    ):
        _invalid()
    base_revision = revision["base_revision"]
    if base_revision is not None:
        if _identifier(base_revision, _REVISION_ID) == revision_id:
            _invalid()
    model = revision["model"]
    if model is not None:
        _artifact(model)
    for artifact in _plain_list(revision["artifacts"]):
        _artifact(artifact)
    return ProjectSummary(
        project_id=project_id,
        generation=_generation(head["generation"]),
        revision_id=revision_id,
        manifest_sha256=manifest,
    )


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


def task_summary_from_detail_mapping(mapping: object) -> TaskSummary:
    result = _plain_dict(_result_from_outer(mapping), _TASK_DETAIL_RESULT_KEYS)
    record = _plain_dict(result["task_run"], _TASK_RUN_KEYS)
    _schema_version(record["schema_version"])
    task_id = _identifier(record["id"], _TASK_ID)
    project_id = _identifier(record["project_id"], _PROJECT_ID)
    base_revision = _identifier(record["base_revision"], _REVISION_ID)
    candidate_revision = _optional_identifier(record["candidate_revision"], _REVISION_ID)
    committed_revision = _optional_identifier(record["committed_revision"], _REVISION_ID)
    creation_digest = record["creation_digest"]
    if creation_digest is not None:
        _identifier(creation_digest, _DIGEST)
    if record["program"] is not None and type(record["program"]) is not dict:
        _invalid()
    for name in ("steps", "verification_reports", "artifacts", "transitions"):
        _plain_list(record[name])
    if record["last_error"] is not None and type(record["last_error"]) is not dict:
        _invalid()
    draft_id: str | None = None
    if record["draft"] is not None:
        draft = _plain_dict(record["draft"], _DRAFT_KEYS)
        _schema_version(draft["schema_version"])
        draft_id = _identifier(draft["id"], _DRAFT_ID)
        draft_revision = _identifier(draft["revision_id"], _REVISION_ID)
        if (
            _identifier(draft["task_id"], _TASK_ID) != task_id
            or _identifier(draft["project_id"], _PROJECT_ID) != project_id
            or _identifier(draft["base_revision"], _REVISION_ID) != base_revision
            or _generation(draft["base_generation"]) < 0
            or _identifier(draft["base_manifest_sha256"], _DIGEST) == ""
            or _identifier(draft["manifest_sha256"], _DIGEST) == ""
            or _identifier(draft["verification_id"], _VERIFICATION_ID) == ""
            or _nonempty_string(draft["acceptance_id"]) == ""
            or _identifier(draft["observation_digest"], _DIGEST) == ""
            or candidate_revision != draft_revision
            or draft_id != f"draft_{draft_revision.removeprefix('revision_')}"
        ):
            _invalid()
    return TaskSummary(
        task_id=task_id,
        project_id=project_id,
        generation=_generation(result["generation"]),
        base_revision=base_revision,
        reasoning_owner=_nonempty_string(record["reasoning_owner"]),
        review_policy=_nonempty_string(record["review_policy"]),
        status=_nonempty_string(record["status"]),
        next_action=_nonempty_string(result["next_action"]),
        candidate_revision=candidate_revision,
        committed_revision=committed_revision,
        draft_id=draft_id,
    )


def _release_uri(value: object, release_id: str, name: str) -> str:
    expected = f"vibecad://release/{release_id}/{name}"
    if type(value) is not str or value != expected:
        _invalid()
    return value


def _release_file(
    value: object,
    *,
    release_id: str,
    name: str,
    nullable_uri: bool = False,
) -> ReleaseFileSummary:
    record = _plain_dict(value, _RELEASE_FILE_KEYS)
    if (
        record["name"] != name
        or record["media_type"] != _RELEASE_MEDIA_TYPES[name]
        or type(record["size_bytes"]) is not int
        or not 0 < record["size_bytes"] <= _MAX_GENERATION
    ):
        _invalid()
    uri = record["resource_uri"]
    if uri is None:
        if not nullable_uri:
            _invalid()
    else:
        uri = _release_uri(uri, release_id, name)
    return ReleaseFileSummary(
        name=name,
        media_type=_RELEASE_MEDIA_TYPES[name],
        sha256=_identifier(record["sha256"], _DIGEST),
        size_bytes=record["size_bytes"],
        resource_uri=uri,
    )


def release_summary_from_mapping(mapping: object) -> ReleaseSummary:
    result = _plain_dict(_result_from_outer(mapping), _RELEASE_KEYS)
    release_id = _identifier(result["release_id"], _RELEASE_ID)
    status = result["status"]
    generation = _generation(result["generation"])
    approved_at_ms = result["approved_at_ms"]
    package = _release_file(
        result["package"],
        release_id=release_id,
        name="vibecad-release.zip",
        nullable_uri=True,
    )
    if status == "draft":
        if generation != 0 or approved_at_ms is not None or package.resource_uri is not None:
            _invalid()
    elif status == "approved":
        if (
            generation != 1
            or type(approved_at_ms) is not int
            or not 0 < approved_at_ms <= _MAX_GENERATION
            or package.resource_uri is None
        ):
            _invalid()
    else:
        _invalid()
    return ReleaseSummary(
        release_id=release_id,
        status=status,
        generation=generation,
        task_id=_identifier(result["task_id"], _TASK_ID),
        task_generation=_generation(result["task_generation"]),
        project_id=_identifier(result["project_id"], _PROJECT_ID),
        revision_id=_identifier(result["revision_id"], _REVISION_ID),
        revision_manifest_sha256=_identifier(result["revision_manifest_sha256"], _DIGEST),
        verification_id=_identifier(result["verification_id"], _VERIFICATION_ID),
        verification_digest=_identifier(result["verification_digest"], _DIGEST),
        observation_digest=_identifier(result["observation_digest"], _DIGEST),
        manifest=_release_file(result["manifest"], release_id=release_id, name="manifest.json"),
        drawing=_release_file(
            result["drawing"], release_id=release_id, name="assembly-drawing.pdf"
        ),
        bom_json=_release_file(result["bom_json"], release_id=release_id, name="bom.json"),
        bom_csv=_release_file(result["bom_csv"], release_id=release_id, name="bom.csv"),
        validation_report_uri=_release_uri(
            result["validation_report_uri"], release_id, "validation-report.json"
        ),
        package=package,
        approved_at_ms=approved_at_ms,
    )
