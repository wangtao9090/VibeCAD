"""S3-8 host-skill, distribution, and release contracts.

These tests intentionally parse the skill and packaging metadata instead of freezing a
full prose document.  Each assertion represents a product claim that a host must be able
to discover without falling back to the retired Session surface.
"""

from __future__ import annotations

import json
import re
import tomllib
from collections.abc import Iterable, Mapping
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
SKILL_ROOT = ROOT / "skills" / "vibecad-agent"
SKILL_FILE = SKILL_ROOT / "SKILL.md"
OPENAI_YAML = SKILL_ROOT / "agents" / "openai.yaml"

PUBLIC_TOOL_NAMES = (
    "ping",
    "get_runtime_status",
    "ensure_runtime",
    "uninstall_runtime",
    "get_capabilities",
    "create_project",
    "get_project",
    "create_task",
    "get_task",
    "submit_model_program",
    "resume_task",
    "accept_draft",
    "reject_draft",
    "export_task_artifacts",
    "create_box",
    "create_cylinder",
    "inspect_model",
    "modify_parameter",
    "move_part",
    "rotate_part",
)

LEGACY_TOOL_NAMES = {
    "smoke_cad",
    "new_document",
    "add_hole",
    "fillet_edges",
    "render_part",
    "set_active_part",
    "export_part",
}

NEXT_ACTIONS = {
    "request_plan",
    "submit_program",
    "validate_program",
    "provide_input",
    "reconcile",
    "cleanup",
    "review_draft",
    "wait",
    "none",
}


def _read(path: Path) -> str:
    assert path.is_file(), f"missing required skill artifact: {path.relative_to(ROOT)}"
    return path.read_text(encoding="utf-8")


def _skill_parts() -> tuple[dict[str, object], str]:
    raw = _read(SKILL_FILE)
    match = re.fullmatch(r"---\n(?P<frontmatter>.*?)\n---\n(?P<body>.*)", raw, re.DOTALL)
    assert match is not None, "SKILL.md must have one YAML frontmatter block at byte zero"
    metadata = yaml.safe_load(match.group("frontmatter"))
    assert isinstance(metadata, dict)
    return metadata, match.group("body")


def _inline_code(text: str) -> set[str]:
    return {value.strip() for value in re.findall(r"(?<!`)`([^`\n]+)`(?!`)", text)}


def _normalized(text: str) -> str:
    return " ".join(text.casefold().split())


def _paragraphs(text: str) -> tuple[str, ...]:
    return tuple(part.strip() for part in re.split(r"\n\s*\n", text) if part.strip())


def _paragraph_with(text: str, *needles: str) -> str:
    for paragraph in _paragraphs(text):
        normalized = _normalized(paragraph)
        if all(needle.casefold() in normalized for needle in needles):
            return paragraph
    raise AssertionError(f"no one paragraph contains all required terms: {needles!r}")


def _fenced_blocks(text: str) -> tuple[str, ...]:
    return tuple(
        match.group("body")
        for match in re.finditer(
            r"^```[^\n]*\n(?P<body>.*?)^```\s*$",
            text,
            flags=re.MULTILINE | re.DOTALL,
        )
    )


def _contains_in_order(text: str, values: Iterable[str]) -> bool:
    offset = 0
    for value in values:
        found = text.find(value, offset)
        if found < 0:
            return False
        offset = found + len(value)
    return True


def _table_rows(text: str) -> tuple[tuple[str, ...], ...]:
    rows: list[tuple[str, ...]] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not (stripped.startswith("|") and stripped.endswith("|")):
            continue
        cells = tuple(cell.strip() for cell in stripped[1:-1].split("|"))
        if cells and not all(re.fullmatch(r":?-{3,}:?", cell) for cell in cells):
            rows.append(cells)
    return tuple(rows)


def _sections(text: str, heading_pattern: str) -> tuple[str, ...]:
    headings = tuple(re.finditer(r"^(#{1,6})\s+(.+?)\s*$", text, re.MULTILINE))
    sections: list[str] = []
    for index, heading in enumerate(headings):
        if re.search(heading_pattern, heading.group(2), re.IGNORECASE):
            level = len(heading.group(1))
            end = len(text)
            for later in headings[index + 1 :]:
                if len(later.group(1)) <= level:
                    end = later.start()
                    break
            sections.append(text[heading.end() : end])
    assert sections, f"missing skill section matching {heading_pattern!r}"
    return tuple(sections)


def _workflow_jobs() -> dict[str, dict[str, object]]:
    raw = yaml.safe_load(_read(ROOT / ".github" / "workflows" / "release.yml"))
    assert isinstance(raw, dict) and isinstance(raw.get("jobs"), dict)
    jobs = raw["jobs"]
    assert all(isinstance(name, str) and isinstance(job, dict) for name, job in jobs.items())
    return jobs


def _job_text(job: Mapping[str, object]) -> str:
    return json.dumps(job, ensure_ascii=False, sort_keys=True)


def _needs(job: Mapping[str, object]) -> set[str]:
    value = job.get("needs", ())
    if isinstance(value, str):
        return {value}
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return set(value)
    assert value in (None, ()), "job needs must be a string or string list"
    return set()


def _dependency_closure(jobs: Mapping[str, Mapping[str, object]], name: str) -> set[str]:
    closure: set[str] = set()
    pending = [name]
    while pending:
        current = pending.pop()
        assert current in jobs, f"unknown release dependency: {current}"
        if current in closure:
            continue
        closure.add(current)
        pending.extend(_needs(jobs[current]))
    return closure


def test_skill_has_canonical_files_and_minimal_trigger_frontmatter():
    metadata, _body = _skill_parts()
    assert set(metadata) == {"name", "description"}
    assert metadata["name"] == "vibecad-agent"
    assert isinstance(metadata["description"], str) and metadata["description"].strip()

    config = yaml.safe_load(_read(OPENAI_YAML))
    assert isinstance(config, dict) and isinstance(config.get("interface"), dict)
    interface = config["interface"]
    assert set(interface) >= {"display_name", "short_description", "default_prompt"}
    assert isinstance(interface["display_name"], str) and interface["display_name"].strip()
    assert 25 <= len(interface["short_description"]) <= 64
    assert "$vibecad-agent" in interface["default_prompt"]


def test_skill_teaches_the_exact_twenty_tool_agent_first_flow():
    _metadata, body = _skill_parts()
    code_tokens = _inline_code(body)
    assert set(PUBLIC_TOOL_NAMES) <= code_tokens
    assert LEGACY_TOOL_NAMES.isdisjoint(code_tokens)
    assert re.search(r"\b20(?:-tool| tools?)\b|20\s*个", body, re.IGNORECASE)

    essential_order = (
        "get_capabilities",
        "create_project",
        "create_task",
        "get_task",
        "export_task_artifacts",
        "resources/read",
    )
    assert any(_contains_in_order(block, essential_order) for block in _fenced_blocks(body))
    _paragraph_with(body, "direct", "ModelProgram")


def test_skill_limits_project_import_to_the_verified_box_cylinder_envelope():
    _metadata, body = _skill_parts()
    paragraph = _paragraph_with(body, "import_fcstd", "Part::Box", "Part::Cylinder")
    normalized = _normalized(paragraph)
    assert "empty" in normalized
    assert any(word in normalized for word in ("reject", "unsupported", "拒绝", "不支持"))

    unsupported = "\n".join(_sections(body, r"unsupported|unavailable|未支持|不可用|限制"))
    unsupported_normalized = _normalized(unsupported)
    for value in ("step", "stl"):
        assert value in unsupported_normalized
    assert any(
        phrase in unsupported_normalized
        for phrase in ("import unavailable", "import unsupported", "导入尚未", "导入不支持")
    )


def test_skill_has_the_exact_executable_next_action_table():
    _metadata, body = _skill_parts()
    action_rows: dict[str, str] = {}
    for row in _table_rows(body):
        if len(row) < 2:
            continue
        row_actions = NEXT_ACTIONS.intersection(_inline_code(row[0]))
        for action in row_actions:
            assert action not in action_rows, f"duplicate next_action row: {action}"
            action_rows[action] = row[1]

    assert set(action_rows) == NEXT_ACTIONS
    request_plan = action_rows["request_plan"]
    request_plan_normalized = _normalized(request_plan)
    assert set(PUBLIC_TOOL_NAMES).intersection(_inline_code(request_plan)) == {"get_task"}
    assert re.search(r"\bonce\b|一次", request_plan, re.IGNORECASE)
    assert re.search(
        r"remain|still exists|persist|仍(?:然)?存在",
        request_plan,
        re.IGNORECASE,
    )
    assert re.search(r"stop|停止", request_plan, re.IGNORECASE)
    assert re.search(r"report|报告", request_plan, re.IGNORECASE)
    assert re.search(
        r"internal(?:-state| state)? mismatch|内部(?:状态)?不一致",
        request_plan_normalized,
        re.IGNORECASE,
    )
    assert "direct" not in request_plan.casefold()
    for action in ("submit_program", "provide_input"):
        assert "submit_model_program" in action_rows[action] or "direct" in action_rows[action]
    for action in ("validate_program", "reconcile", "cleanup"):
        assert "resume_task" in action_rows[action]
    assert {"get_task", "resume_task"} <= _inline_code(action_rows["wait"])
    assert {"accept_draft", "reject_draft"} <= _inline_code(action_rows["review_draft"])
    assert re.search(r"stop|停止", action_rows["none"], re.IGNORECASE)

    unknown = _paragraph_with(body, "create_task", "unknown")
    normalized = _normalized(unknown)
    assert "task id" in normalized or "task_id" in normalized
    assert "same retained create key" in normalized
    assert re.search(r"\bretry\b|重试", unknown, re.IGNORECASE)
    assert re.search(
        r"never.{0,32}replacement key|不得.{0,32}新(?:的)? key|不能.{0,32}新(?:的)? key",
        unknown,
        re.IGNORECASE,
    )


def test_skill_teaches_resource_links_and_fail_closed_product_limits():
    _metadata, body = _skill_parts()
    resource = _paragraph_with(body, "ResourceLink", "resources/read")
    resource_normalized = _normalized(resource)
    assert "export_task_artifacts" in resource_normalized
    assert "hash" in resource_normalized or "sha256" in resource_normalized

    path_rule = next(
        (
            paragraph
            for paragraph in _paragraphs(body)
            if "path" in paragraph.casefold()
            and re.search(r"arbitrary|任意", paragraph, re.IGNORECASE)
        ),
        None,
    )
    assert path_rule is not None
    assert re.search(r"never|must not|禁止|不得|不能", path_rule, re.IGNORECASE)

    legacy_rule = _paragraph_with(body, "legacy", "31")
    assert re.search(r"never|must not|禁止|不得|不能", legacy_rule, re.IGNORECASE)
    code_rule = _paragraph_with(body, "Python", "FreeCAD", "code")
    assert re.search(r"never|must not|禁止|不得|不能", code_rule, re.IGNORECASE)

    unsupported = "\n".join(_sections(body, r"unsupported|unavailable|未支持|不可用|限制"))
    normalized = _normalized(unsupported)
    for claim in (
        "mcp_sampling",
        "byok",
        "workbench",
        "face/edge",
        "stl",
        "photo",
        "simulation",
    ):
        assert claim in normalized


def test_skill_documents_host_installation_without_claiming_automatic_activation():
    _metadata, body = _skill_parts()
    required_paths = {
        "$CODEX_HOME/skills/vibecad-agent",
        "$HOME/.codex/skills/vibecad-agent",
        "$HOME/.agents/skills/vibecad-agent",
        ".agents/skills/vibecad-agent",
        "$HOME/.claude/skills/vibecad-agent",
        ".claude/skills/vibecad-agent",
    }
    assert required_paths <= _inline_code(body)

    activation = _paragraph_with(body, "MCPB", "activation")
    assert re.search(r"not|never|不", activation, re.IGNORECASE)
    assert re.search(r"reload|restart|重启|重新加载", body, re.IGNORECASE)
    _paragraph_with(body, "$CODEX_HOME", "tested")
    _paragraph_with(body, ".agents/skills", "published")


def test_skill_distribution_channels_are_explicit_and_non_overlapping():
    with (ROOT / "pyproject.toml").open("rb") as handle:
        pyproject = tomllib.load(handle)
    targets = pyproject["tool"]["hatch"]["build"]["targets"]
    assert targets["wheel"]["packages"] == ["src/vibecad"]
    assert "skills/vibecad-agent" not in json.dumps(targets["wheel"], sort_keys=True)

    sdist_patterns = targets["sdist"].get("include", ())
    assert isinstance(sdist_patterns, list)
    assert any("skills/vibecad-agent" in pattern for pattern in sdist_patterns)

    ignored = {
        line.strip()
        for line in _read(ROOT / ".mcpbignore").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }
    assert not any(pattern.startswith("skills") for pattern in ignored)


def test_manifest_projection_and_all_package_versions_target_0_5_0():
    from vibecad.application.public_surface import public_tool_specs
    from vibecad.runtime import spec

    manifest = json.loads(_read(ROOT / "manifest.json"))
    declared = tuple((entry["name"], entry["description"]) for entry in manifest["tools"])
    projected = tuple((tool.name, tool.description) for tool in public_tool_specs())
    assert tuple(name for name, _description in declared) == PUBLIC_TOOL_NAMES
    assert declared == projected
    assert all(isinstance(description, str) and description.strip() for _, description in declared)

    with (ROOT / "pyproject.toml").open("rb") as handle:
        project_version = tomllib.load(handle)["project"]["version"]
    source = _read(ROOT / "src" / "vibecad" / "__init__.py")
    source_version = re.search(r'^__version__ = "([^"]+)"$', source, re.MULTILINE)
    assert source_version is not None
    assert manifest["version"] == project_version == source_version.group(1) == "0.5.0"
    assert spec.VIBECAD_VERSION == "0.5.0"


def test_release_publishers_consume_gated_archives_and_attach_the_skill_asset():
    jobs = _workflow_jobs()
    pypi_jobs = {
        name for name, job in jobs.items() if "pypa/gh-action-pypi-publish" in _job_text(job)
    }
    release_jobs = {name for name, job in jobs.items() if "gh release create" in _job_text(job)}
    assert len(pypi_jobs) == len(release_jobs) == 1

    for publisher in pypi_jobs | release_jobs:
        publisher_text = _job_text(jobs[publisher])
        closure = _dependency_closure(jobs, publisher)
        closure_text = "\n".join(_job_text(jobs[name]) for name in sorted(closure))

        assert "actions/download-artifact" in publisher_text
        assert "uv build" not in publisher_text
        assert "mcpb@2.1.2 pack" not in publisher_text
        assert "ruff check" in closure_text
        assert "pytest" in closure_text
        assert "uv build" in closure_text
        assert "mcpb@2.1.2 pack" in closure_text
        assert "actions/upload-artifact" in closure_text
        assert "macos" in closure_text.casefold()
        assert "VIBECAD_RUN_INTEGRATION" in closure_text
        assert re.search(r"pytest[^\n]*-m\s+slow", closure_text)

    release_text = _job_text(jobs[next(iter(release_jobs))])
    assert "VibeCAD.mcpb" in release_text
    assert "vibecad-agent-skill-" in release_text
