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
from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
SKILL_ROOT = ROOT / "skills" / "vibecad-agent"
SKILL_FILE = SKILL_ROOT / "SKILL.md"
OPENAI_YAML = SKILL_ROOT / "agents" / "openai.yaml"
PARAMETRIC_REFERENCE = SKILL_ROOT / "references" / "parametric-design-ir-v1.md"
GUIDED_PHOTO_REFERENCE = SKILL_ROOT / "references" / "guided-photo-v1.md"
S35_VISUAL_FIXTURES = (
    "visual-cad-l-bracket-front",
    "visual-cad-l-bracket-right",
    "visual-cad-l-bracket-top",
    "visual-cad-depth-ambiguous-front",
    "visual-cad-depth-ambiguous-isometric",
    "visual-cad-conflict-front-50",
    "visual-cad-conflict-top-45",
)

PUBLIC_TOOL_NAMES = (
    "ping",
    "get_runtime_status",
    "ensure_runtime",
    "uninstall_runtime",
    "get_capabilities",
    "create_project",
    "get_project",
    "list_projects",
    "list_revisions",
    "compare_revisions",
    "revert_project",
    "create_task",
    "list_tasks",
    "get_task",
    "get_task_events",
    "submit_model_program",
    "resume_task",
    "cancel_task",
    "accept_draft",
    "reject_draft",
    "get_artifact_manifest",
    "export_task_artifacts",
    "create_release",
    "get_release",
    "approve_release",
    "create_reconstruction",
    "get_reconstruction",
    "run_reconstruction",
    "answer_reconstruction",
    "adopt_reconstruction",
    "reject_reconstruction",
    "delete_reconstruction",
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


def test_skill_teaches_the_exact_thirty_eight_tool_agent_first_flow():
    _metadata, body = _skill_parts()
    code_tokens = _inline_code(body)
    assert set(PUBLIC_TOOL_NAMES) <= code_tokens
    assert LEGACY_TOOL_NAMES.isdisjoint(code_tokens)
    assert re.search(r"\b38(?:-tool| tools?)\b|38\s*个", body, re.IGNORECASE)

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


def test_skill_routes_visual_reconstruction_without_claiming_attachment_ingress():
    _metadata, body = _skill_parts()
    visual = "\n".join(_sections(body, r"visual reconstruction|视觉重建"))
    normalized = _normalized(visual)

    assert {
        "create_reconstruction",
        "get_reconstruction",
        "run_reconstruction",
        "answer_reconstruction",
        "adopt_reconstruction",
        "reject_reconstruction",
        "delete_reconstruction",
        "image_set_id",
        "image_set_manifest_sha256",
    } <= _inline_code(visual)
    assert "trusted local host adapter" in normalized
    assert "workbuddy direct attachment" in normalized
    assert re.search(r"not verified|unverified|未验证", visual, re.IGNORECASE)
    for forbidden in ("path", "base64", "resource uri"):
        assert forbidden in normalized
    assert re.search(r"never|must not|禁止|不得|不能", visual, re.IGNORECASE)


def test_skill_freezes_the_multi_view_mechanical_envelope_and_safe_failures():
    _metadata, body = _skill_parts()
    visual = "\n".join(_sections(body, r"host-owned image-to-cad"))
    normalized = _normalized(visual)

    for required in (
        "two to sixteen",
        "same object, state, and scale",
        "evidence matrix",
        "cross_view_derived",
        "distinct known view roles",
        "stop before `create_task`",
        "extrusion depth",
        "disagree",
        "at most 16",
        "multi-loop pocket",
    ):
        assert required in normalized
    _paragraph_with(visual, "multi-location", "same sketch plane", "location_geometry_ids")

    reference = _normalized(_read(PARAMETRIC_REFERENCE))
    assert "1–16 nonconstruction circles" in reference
    assert "sequential one-wire pocket sketches" in reference
    assert "material-removal point on every declared axis" in reference
    assert "world -y" in reference
    assert "same parameter id" in reference
    assert "exactly four fields" in reference
    skill = _normalized(body)
    assert "independent constraint set" in skill
    assert "axis: null" in skill
    assert "separate radius parameter" in skill


def test_s35_visual_fixtures_are_self_contained_metadata_free_png_and_svg_pairs():
    image_root = ROOT / "docs" / "images"
    for stem in S35_VISUAL_FIXTURES:
        svg = image_root / f"{stem}.svg"
        png = image_root / f"{stem}.png"
        assert svg.is_file() and png.is_file()
        raw = svg.read_text(encoding="utf-8")
        assert raw.startswith('<svg xmlns="http://www.w3.org/2000/svg"')
        assert 'viewBox="0 0 1200 1200"' in raw
        with Image.open(png) as image:
            assert image.format == "PNG"
            assert image.size == (1200, 1200)
            assert not image.getexif()
            assert not image.info

    positive = {
        role: (image_root / f"visual-cad-l-bracket-{role}.svg").read_text(encoding="utf-8")
        for role in ("front", "right", "top")
    }
    assert all("SCALE 4:1 · SAME PART / STATE / SCALE" in raw for raw in positive.values())
    assert "(X1,Z1)=(22,18) · (X2,Z2)=(36,42)" in positive["front"]
    assert "(Y,Z)=(24,30)" in positive["right"]
    assert "constant L-section extruded 60 mm" in positive["top"]


def test_skill_routes_host_visible_images_through_the_existing_task_kernel():
    metadata, body = _skill_parts()
    assert "image" in metadata["description"].casefold()

    host_vision = "\n".join(
        _sections(body, r"host-owned image-to-cad|宿主.*图片.*cad|宿主.*图像.*cad")
    )
    normalized = _normalized(host_vision)
    assert {"create_task", "submit_model_program", "run_reconstruction"} <= _inline_code(
        host_vision
    )
    assert "multimodal" in normalized
    assert "already visible" in normalized
    assert re.search(
        r"do not call.{0,48}`run_reconstruction`|不得.{0,48}`run_reconstruction`",
        host_vision,
        re.IGNORECASE | re.DOTALL,
    )
    assert all(term in normalized for term in ("confirmed", "inferred", "unknown"))
    assert "absolute" in normalized and "scale" in normalized
    assert "require_review" in normalized
    assert re.search(r"api key|api token|provider credential", normalized)


def test_skill_guides_scale_backed_photos_without_opening_a_premature_task():
    _metadata, body = _skill_parts()
    assert "references/guided-photo-v1.md" in body
    assert GUIDED_PHOTO_REFERENCE.is_file()

    guided = "\n".join(_sections(body, r"guided ordinary photos|guided photo"))
    normalized = _normalized(guided)
    for required in (
        "photo_ready",
        "needs_capture",
        "out_of_envelope",
        "same physical plane",
        "discard the provisional plan",
        "stop before `create_task`",
    ):
        assert required in normalized

    reference = _normalized(_read(GUIDED_PHOTO_REFERENCE))
    for required in (
        "single rigid mechanical part",
        "background separation",
        "profile-normal",
        "depth-normal",
        "axis-normal",
        "coplanar scale reference",
        "direct user measurement",
        "one concrete recapture or measurement request",
        "geometry-completeness gate",
        "rebuild the evidence matrix",
        "require_review",
        "do not create a proportional cad placeholder",
    ):
        assert required in reference
    assert len(_read(GUIDED_PHOTO_REFERENCE).encode("utf-8")) < 16_000


def test_skill_carries_the_portable_parametric_ir_authoring_contract():
    _metadata, body = _skill_parts()
    assert "references/parametric-design-ir-v1.md" in body
    assert PARAMETRIC_REFERENCE.is_file()
    reference = PARAMETRIC_REFERENCE.read_text(encoding="utf-8")
    normalized = _normalized(reference)
    for required in (
        "create_parametric_design",
        "parametric_design_ir",
        "the complete modelprogram root",
        "modelcommand, not a complete modelprogram",
        '"task_id": "task_id"',
        '"base_revision": "base_revision"',
        '"operations"',
        '"acceptance"',
        "confirmed",
        "cross_view_derived",
        "ir_<kind>_<32 lowercase hex>",
        "ir_parameter_",
        "ir_geometry_",
        "ir_constraint_",
        "ir_feature_",
        "zero-padded counters",
        "copy that exact declared string",
        "byte-for-byte present in the declarations",
        "31 or 33 hex digits",
        "source_refs[1+]",
        "user:current_request",
        "never add an unused convenience datum",
        "serialize the final `program_json` compactly",
        "schema_version: 1",
        "hole_locations",
        "end-cap centers",
        "two native lines",
        "oblique slots fail closed",
        "do not attach ir constraints to a slot",
        "verified independent-coordinate recipe",
        "16 independent line constraints",
        "20 independent arc constraints",
        "add no `coincident`, `tangent`, or `equal` constraints",
        "bottom-right `start_x = center_x`",
        "never constrain the radial extreme coordinates",
        "at most 128 parameters",
        "through_all",
        "revolve",
        "edge_treatments",
        "section_start|section_end",
        "linear radius law",
        "variable chamfer",
        "never use `edgen`",
        "dof=0",
        "valid_shape: true",
        "solid_count: 1",
    ):
        assert required in normalized
    assert "inferred" in normalized and "outside the ir" in normalized
    assert "unscaled image" in normalized and "absolute millimetres" in normalized
    for abbreviated_prefix in ("ir_param_", "ir_geom_", "ir_const_", "ir_feat_"):
        assert abbreviated_prefix in normalized and "invalid" in normalized
    assert all(
        f'"check": "{check}"' in reference
        for check in ("bbox", "volume", "valid_shape", "solid_count")
    )
    assert 'target: "@origin"' in reference and 'point: "center"' in reference
    assert "reference order is semantic" in normalized
    assert "the first reference" in normalized and "the second" in normalized
    assert "do not reverse them" in normalized
    assert "empty `source_refs` array" in reference
    assert "without indentation or insignificant whitespace" in reference
    assert len(reference.encode("utf-8")) < 20_000


def test_skill_documents_bounded_workbuddy_deferred_tool_permissions():
    _metadata, body = _skill_parts()
    installation = "\n".join(_sections(body, r"host installation|宿主安装"))
    normalized = _normalized(installation)
    assert {"ToolSearch", "DeferExecuteTool"} <= _inline_code(installation)
    assert "headless" in normalized
    assert "exact vibecad operations" in normalized
    assert re.search(r"do not disable permission checks", installation, re.IGNORECASE)
    assert "vibecad --workbuddy-submit" in installation
    assert ".vibecad-workbuddy-request-<name>.json" in installation
    assert {"schema_version", "task_id", "expected_generation", "program"} <= _inline_code(
        installation
    )
    assert "not an escaped string" in normalized
    assert "cannot bypass the task kernel" in normalized
    assert "exact error path" in normalized
    assert "unbounded repair loop" in normalized


def test_skill_requires_exact_distinct_project_and_task_idempotency_keys():
    _metadata, body = _skill_parts()
    normalized = _normalized(body)
    assert "project_create_[0-9a-f]{32}" in body
    assert "task_create_[0-9a-f]{32}" in body
    assert "a different fresh key" in normalized
    assert "not labels encoded or padded by hand" in normalized


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


def test_skill_distinguishes_durable_task_cancel_from_transport_cancellation():
    _metadata, body = _skill_parts()
    paragraph = _paragraph_with(body, "cancel_task", "notifications/cancelled")
    normalized = _normalized(paragraph)
    assert {
        "created",
        "needs_plan",
        "program_ready",
        "needs_input",
        "cancel_requested",
        "cancelling",
        "cancelled",
        "reject_draft",
    } <= _inline_code(paragraph)
    assert "exact persisted generation" in normalized
    assert "resume_task" in _inline_code(paragraph)
    assert "next_action" in normalized
    assert "reconcile" in normalized
    assert "worker generation" in normalized
    assert "not durable task cancellation" in normalized


def test_skill_teaches_resource_links_and_fail_closed_product_limits():
    _metadata, body = _skill_parts()
    resource = _paragraph_with(
        body,
        "ResourceLink",
        "resources/read",
        "export_task_artifacts",
    )
    resource_normalized = _normalized(resource)
    assert "export_task_artifacts" in resource_normalized
    assert any(token in resource_normalized for token in ("hash", "sha256", "sha-256"))

    review_resource = _paragraph_with(
        body,
        "review_resources",
        "ResourceLink",
        "resources/read",
    )
    review_normalized = _normalized(review_resource)
    assert "advisory_only" in review_normalized
    assert "png" in review_normalized
    assert "sha-256" in review_normalized
    assert re.search(r"cannot|never|must not|禁止|不得|不能", review_resource, re.IGNORECASE)

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

    retired_rule = _paragraph_with(body, "retired", "38")
    assert re.search(r"never|must not|禁止|不得|不能", retired_rule, re.IGNORECASE)
    code_rule = _paragraph_with(body, "Python", "FreeCAD", "code")
    assert re.search(r"never|must not|禁止|不得|不能", code_rule, re.IGNORECASE)

    unsupported = "\n".join(_sections(body, r"unsupported|unavailable|未支持|不可用|限制"))
    normalized = _normalized(unsupported)
    for claim in (
        "mcp_sampling",
        "byok",
        "face/edge",
        "stl",
        "photo",
        "simulation",
    ):
        assert claim in normalized


def test_skill_teaches_honest_revision_compare_and_read_only_manifest_routing():
    _metadata, body = _skill_parts()

    comparison = _paragraph_with(body, "compare_revisions", "ancestry")
    comparison_normalized = _normalized(comparison)
    assert "hash" in comparison_normalized or "sha256" in comparison_normalized
    assert "size" in comparison_normalized
    assert re.search(
        r"unsupported|不支持|不可用",
        comparison,
        re.IGNORECASE,
    )
    assert any(
        scope in comparison_normalized
        for scope in ("geometry", "entity", "parameter", "几何", "实体", "参数")
    )

    manifest = _paragraph_with(
        body,
        "get_artifact_manifest",
        "export_task_artifacts",
        "materialized",
    )
    manifest_normalized = _normalized(manifest)
    assert "materialized" in manifest_normalized
    assert "resources/read" in manifest_normalized
    assert re.search(
        r"only|仅|才",
        manifest,
        re.IGNORECASE,
    )


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


def test_manifest_projection_and_all_package_versions_target_0_9_0():
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
    with (ROOT / "uv.lock").open("rb") as handle:
        lock = tomllib.load(handle)
    locked = [package["version"] for package in lock["package"] if package.get("name") == "vibecad"]
    assert locked == ["0.9.0"]
    assert manifest["version"] == project_version == source_version.group(1) == "0.9.0"
    assert spec.VIBECAD_VERSION == "0.9.0"


def test_release_documents_project_the_0_9_0_backend_truth():
    documents = {
        path: _normalized(_read(ROOT / path))
        for path in (
            "README.md",
            "README.zh-CN.md",
            "PRIVACY.md",
            "docs/ARCHITECTURE.md",
            "docs/AGENT_ARCHITECTURE.md",
            "docs/PRODUCT_CAPABILITY_ROADMAP.md",
            "docs/USER_GUIDE.md",
            "docs/ACCEPTANCE_TESTS.md",
        )
    }
    for path, normalized in documents.items():
        assert "0.5.0" not in normalized, path
        assert "0.9.0" in normalized, path
        assert "27-tool" not in normalized, path
        assert "27 个工具" not in normalized, path

    product_documents = {
        path: documents[path]
        for path in (
            "README.md",
            "README.zh-CN.md",
            "docs/ARCHITECTURE.md",
            "docs/AGENT_ARCHITECTURE.md",
            "docs/PRODUCT_CAPABILITY_ROADMAP.md",
            "docs/USER_GUIDE.md",
            "docs/ACCEPTANCE_TESTS.md",
        )
    }
    for path, normalized in product_documents.items():
        assert any(claim in normalized for claim in ("38-tool", "38 个工具", "38 个公开工具")), path
        assert "daemon" in normalized, path
        assert "task kernel" in normalized, path

    english_readme = documents["README.md"]
    assert "freecad workbench alpha" in english_readme
    assert "g1 (alpha complete)" in english_readme
    assert "exact object/feature selector capture" in english_readme
    assert "not general system-freecad support" in english_readme

    chinese_readme = documents["README.zh-CN.md"]
    assert "freecad workbench alpha" in chinese_readme
    assert "g1（alpha 完成）" in chinese_readme
    assert "精确 object/feature selector 捕获" in chinese_readme
    assert "不是通用的系统 freecad 支持" in chinese_readme

    assert "g1 freecad workbench alpha 已交付" in documents["docs/ARCHITECTURE.md"]
    assert "g1 mvp 已在这个范围交付" in documents["docs/AGENT_ARCHITECTURE.md"]
    assert "g1 workbench alpha 已完成" in documents["docs/PRODUCT_CAPABILITY_ROADMAP.md"]
    assert "真实 freecad qt workbench alpha 已交付" in documents["docs/USER_GUIDE.md"]
    assert "g1 freecad qt workbench alpha 支持" in documents["docs/ACCEPTANCE_TESTS.md"]

    assert "p2 (complete boundary)" in english_readme
    assert "workbuddy (verified)" in english_readme
    assert "p2（有界完成）" in chinese_readme
    assert "workbuddy（已验证）" in chinese_readme

    acceptance = documents["docs/ACCEPTANCE_TESTS.md"]
    assert "0.8.0 已交付的 guided photo v3 继续作为回归门" in acceptance
    assert "0.9.0 的新增门是 s41 派生参数联动与 s42 语义 fillet/chamfer" in acceptance
    assert "本次新增的 guided photo v3" not in acceptance

    product_strategy = _normalized(_read(ROOT / "docs" / "PRODUCT_STRATEGY.md"))
    assert "g1 freecad workbench 尚未交付" not in product_strategy
    assert "mr0 多 runtime 合同尚未实现" not in product_strategy
    assert "真实 claude code/codex 尚未" not in product_strategy
    assert "先完成 mr0 internal foundation" not in product_strategy

    assert "on the current visual branch" not in english_readme
    assert "当前 visual branch" not in chinese_readme

    release_notes = _normalized(_read(ROOT / "docs" / "releases" / "v0.9.0.md"))
    assert release_notes.startswith("# vibecad v0.9.0 ")
    assert "## highlights" in release_notes
    assert "derived design parameter" in release_notes
    assert "fillet or chamfer" in release_notes
    assert "linear start-to-end radius law" in release_notes
    assert "## boundaries" in release_notes
    assert "arbitrary imported step edge treatment" in release_notes
    assert "freeform surfaces" in release_notes
    assert "sculpture" in release_notes
    assert "general step/stl import" in release_notes
    assert "## upgrade" in release_notes
    assert "vibecad==0.9.0" in release_notes
    assert "vibecad-agent-skill-0.9.0.zip" in release_notes
    assert "epoch stays at 4" in release_notes
    assert "tool count stays at 38" in release_notes


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
