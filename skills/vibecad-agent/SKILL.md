---
name: vibecad-agent
description: Use VibeCAD's Agent-first MCP surface to turn text or host-visible image references into verified FreeCAD projects and tasks, then inspect, modify, review, and export FCStd/STEP artifacts. Use for bounded CAD work that must remain recoverable and auditable without executing arbitrary Python or FreeCAD code; image understanding stays with the calling multimodal host.
---

# VibeCAD Agent

Use the current 38-tool Agent-first surface. Treat VibeCAD's persisted project, task, revision, draft, visual reconstruction, evidence, artifact, and release records as authoritative. Keep model reasoning, image understanding, subscription, and credentials with the calling host. Never infer success from prose alone.

## Public tools

Runtime and capability tools: `ping`, `get_runtime_status`, `ensure_runtime`, `uninstall_runtime`, `get_capabilities`.

Project, task, and delivery tools: `create_project`, `get_project`, `list_projects`, `list_revisions`, `compare_revisions`, `revert_project`, `create_task`, `list_tasks`, `get_task`, `get_task_events`, `submit_model_program`, `resume_task`, `cancel_task`, `accept_draft`, `reject_draft`, `get_artifact_manifest`, `export_task_artifacts`, `create_release`, `get_release`, `approve_release`.

Visual reconstruction tools: `create_reconstruction`, `get_reconstruction`, `run_reconstruction`, `answer_reconstruction`, `adopt_reconstruction`, `reject_reconstruction`, `delete_reconstruction`.

Direct CAD tools: `create_box`, `create_cylinder`, `inspect_model`, `modify_parameter`, `move_part`, `rotate_part`.

Use a direct tool for one supported operation with explicit inputs. Use ModelProgram for an ordered multi-command change. Both direct and ModelProgram paths enter the same Task Kernel, so recovery, verification, review, and acceptance semantics stay identical.

Project, task, revision, review, artifact, release, and CAD MCP calls plus the public Workbench client use one same-user authenticated local daemon and shared Task Kernel. Runtime maintenance and inert discovery remain local MCP server concerns. FreeCAD runs behind the kernel in a managed, killable Worker generation. The G1 FreeCAD Workbench alpha uses this same authority for review and Release actions.

## Required workflow

Initialize or verify the runtime first. Once it is ready, call `get_capabilities` as the first business discovery tool instead of guessing CAD support or arguments. Keep every write attached to the returned project id, task id, generation, base revision, draft revision, and idempotency key.

Runtime maintenance is never a schema-recovery or task-recovery mechanism. Call
`ensure_runtime` only when `get_runtime_status` says the runtime is not ready.
Never call `uninstall_runtime` unless the user explicitly asks to remove the
runtime after reviewing its preview; a host's broad tool permission or an
autonomous execution mode is not user confirmation.

```text
get_runtime_status
  -> ensure_runtime only when the runtime is not ready
  -> get_capabilities
  -> create_project
  -> create_task
  -> get_task
  -> route the persisted next_action until review or terminal state
  -> get_artifact_manifest
  -> export_task_artifacts only when no verified delivery is materialized
  -> resources/read for each returned resource URI
```

Before `create_project`, generate and retain one fresh key matching
`project_create_[0-9a-f]{32}`. Before `create_task`, generate and retain a
different fresh key matching `task_create_[0-9a-f]{32}`. These are exactly 32
lowercase hexadecimal characters after the prefix, not labels encoded or
padded by hand. Before another mutating call, read the current task when state
may have advanced. After a mutating call, use the returned state and
generation; do not replay merely because a response is slow. Accept only the
named draft based on its evidence, or reject that exact draft explicitly.

Use `cancel_task` first with the exact persisted generation. It immediately cancels idle `created`, `needs_plan`, `program_ready`, or `needs_input` tasks; for active work it durably records `cancel_requested`, fences the current Worker generation, advances through `cancelling`, and reconciles to a proved `cancelled`, committed, recovery, or cleanup result. If the response is unknown, replay the identical request instead of inventing a future generation. When the returned persisted `next_action` is `reconcile`, call `get_task`, then call `resume_task` at most once with that exact observed generation; never infer that the Worker generation stopped or that cancellation succeeded from elapsed time. A task already in `cancel_requested`, `cancelling`, or `cancelled` returns its current durable state. An awaiting review draft must use `reject_draft`. MCP `notifications/cancelled` only cancels one transport request and is not durable task cancellation.

Use `list_tasks` only to recover an existing task when its id is unknown: page through bounded summaries, choose the intended task, then call `get_task`. If a snapshot cursor returns `conflict`, discard it and restart from the first page. Use `get_task_events` only to audit the ordered persisted `TaskRun.transitions`; it is not a timestamped log. If its cursor becomes stale, restart that task's event pagination from the first page.

Use `list_projects` only when the project id is unknown, then call `get_project` for the authoritative current HEAD. Use `list_revisions` only for the committed ancestry of that current HEAD. Its page is sorted by canonical revision id, not time; reconstruct lineage from the returned `head` and each `base_revision`. Drafts, candidates, and abandoned revisions are excluded. On either cursor `conflict`, discard it and restart from page one. These read-only discovery calls do not run CAD, construct a runtime, or acquire a project write lease.

Use `compare_revisions` only for two revisions in that current committed ancestry. It verifies lineage plus revision-manifest and FCStd/STEP presence, identifiers, hashes, and sizes. Its `semantic_diff.status` is always `unsupported`: file differences are not proof of a geometry, entity, parameter, or design-intent difference.

Use `revert_project` only with a source revision in that committed ancestry and the exact current HEAD. It copies the historical model into a new verified draft based on the current HEAD; it never rewrites history or restores an old file in place. Inspect and explicitly accept or reject the returned draft through the ordinary review flow.

`create_project` supports `empty` or `import_fcstd`; the verified `import_fcstd` envelope accepts only a nonempty FCStd whose objects are all `Part::Box` or `Part::Cylinder`, and must reject every unsupported or mixed object type.

### Route `next_action`

| Persisted action | Required behavior |
|---|---|
| `request_plan` | Call `get_task` once; if the action remains, stop and report an internal-state mismatch. |
| `submit_program` | Submit the prepared bounded program with `submit_model_program`, or use the matching direct operation when the task contract permits it. |
| `provide_input` | Supply the requested bounded input through `submit_model_program`, or use the matching direct operation when the task contract permits it. |
| `validate_program` | Continue the persisted transition with `resume_task`. |
| `reconcile` | Read the current task, then continue the exact persisted generation once with `resume_task`; this also settles `cancel_requested` or `cancelling` after the Worker generation has been fenced. |
| `cleanup` | Continue the persisted transition with `resume_task`. |
| `review_draft` | Inspect the evidence, then call either `accept_draft` or `reject_draft`. |
| `wait` | Poll with `get_task`; if the persisted state is resumable, call `resume_task` at most once for that observed generation. |
| `none` | Stop mutation and report the terminal or non-actionable state. |

If the outcome of `create_task` is unknown and no task id or task_id was received, retry `create_task` with the exact same retained create key, project id, and review policy. The replay returns the same task's current generation; never generate a replacement key for recovery.

## Host-owned image-to-CAD

When user images are already visible to the calling host's multimodal model, analyze them in the host and use the ordinary Task Kernel. Do not call `run_reconstruction`, request an `image_set_id`, or ask VibeCAD to upload the same images to another model. Do not pass an API key, API token, or provider credential to VibeCAD.

Use clear, complementary views of the same object, state, and scale. Prefer a roughly 2,048-pixel overview plus source-resolution crops for dimension text, small holes, threads, and local boundaries when the host supports image preparation; do not assume that sending every available image improves the result. Classify reconstruction facts before creating CAD:

- `confirmed`: directly readable dimensions, explicit scale evidence, counts, or geometry consistently visible across the supplied views;
- `inferred`: plausible geometry not directly measured; require user confirmation when it changes the CAD result;
- `unknown`: occluded, blurred, conflicting, or absent evidence; ask the user instead of inventing a value.

Never infer an absolute dimension from an unscaled photo. Ask only for missing facts that block a safe parameterized model. Once the required dimensions and feature relationships are sufficient, call `get_capabilities`, create or select the project, call `create_task` with `require_review`, and submit the bounded construction through `submit_model_program`. Continue through the persisted task `next_action`, deterministic verification, draft review, and artifact workflow exactly as for a text request.

Before authoring a `create_parametric_design` command, read `references/parametric-design-ir-v1.md`. `get_capabilities` exposes the operation and its `parametric_design_ir` value shape but not the nested wire contract; the reference is the portable host-side authoring contract. Do not improvise omitted fields, enum values, identity formats, feature order, or evidence states.

The CAD task and ModelProgram become durable after submission; the host's original attachment does not automatically become VibeCAD durable evidence. If a host restart loses image or clarification context, ask the user to reattach or restate the missing evidence. Never reconstruct dimensions from a task id alone.

## Optional sealed-image visual reconstruction

Use the seven visual-reconstruction tools only when an explicitly selected VibeCAD-managed workflow needs a durable local ImageSet or optional Provider lifecycle. A trusted local host adapter must first seal one to sixteen JPEG/PNG inputs and return the exact `image_set_id` and `image_set_manifest_sha256`. WorkBuddy direct attachment ingress into that sealed store is not verified and is not required for host-owned image-to-CAD. Never put a path, Base64 value, image bytes, filename, or Resource URI into a reconstruction MCP request, and never invent or alter either ImageSet binding.

Create the draft with `create_reconstruction`, one retained key matching `reconstruction_create_[0-9a-f]{32}`, the target project, and the exact sealed ImageSet binding. Replay an unknown create outcome only with that identical request. Use `get_reconstruction` to recover the current generation and route only its persisted `next_action`: call `run_reconstruction` for `run`, answer the named bounded question with `answer_reconstruction` for `answer`, and present the proposal summary to the user for `adopt_or_reject`. Pass the exact observed generation to every mutation. A run must provide both its budget and deadline or set both to null.

Call `adopt_reconstruction` only after the user chooses the displayed proposal. Adoption creates an ordinary `REQUIRE_REVIEW` CAD Task; it does not accept a draft or advance project HEAD. Continue that returned task through the normal task/review workflow. Use `reject_reconstruction` to retain a rejected record, or `delete_reconstruction` only when the user wants the draft and its bound local image source removed. Do not treat the deterministic default provider as real photo-to-CAD inference, and do not make this optional lifecycle the default when the host already sees the images.

## Artifact delivery

Call `get_artifact_manifest` first with the exact task generation, revision, and nullable draft binding. If it returns `materialized=true`, consume its typed `ResourceLink` entries and call `resources/read` for their URIs. If it returns `materialized=false`, call `export_task_artifacts` once with a retained export key, then consume its returned `ResourceLink` entries through `resources/read`. The manifest query is read-only: never expect it to create, copy, validate, or repair a delivery. Verify format, byte size, and SHA-256 evidence before handing off the FCStd and STEP files.

Never request, expose, or read an arbitrary filesystem path. Artifact access must use the verified resource URI returned by VibeCAD.

## Release delivery

Create a Release only from a `succeeded` task's exact committed Revision and observed task generation. Call `create_release` with one retained `release_create_[0-9a-f]{32}` key, then inspect the returned drawing, BOM JSON/CSV, manifest, validation report, Revision digest, verification digest, and package digest. A draft exposes preview `ResourceLink` entries but never exposes the ZIP resource URI.

Approval is a separate user decision. After the user approves the exact displayed package SHA-256, call `approve_release` with the draft generation, that unchanged digest, and one retained `release_approve_[0-9a-f]{32}` key. Only an approved Release exposes `vibecad-release.zip`; retrieve it with `resources/read`. Re-read with `get_release` after restart or an unknown response, and replay only the identical idempotency key and expected digest. Release approval never changes Revision or project HEAD.

The current buffered Release resource ceiling is 64 MiB. If creation returns `resource_exhausted`, report that transport limit; do not bypass it with an arbitrary filesystem path or claim that a larger package was approved.

Never reconstruct retired tool names. Use only the live 38-tool surface above.

Never generate or execute arbitrary Python/FreeCAD code. FreeCAD is the bounded geometry engine behind VibeCAD, not an authorization to run model-generated code.

## Unsupported and unavailable capabilities

STEP and STL import unavailable in the verified current envelope; only FCStd Box/Cylinder import is supported. Do not claim `mcp_sampling`, `byok`, Workbench UI, `face/edge` selection, STL reconstruction, universal or release-verified photo reconstruction, or simulation. Host-owned image reasoning is verified only for bounded pilot fixtures when the calling multimodal host can actually see the attachments; VibeCAD-managed visual reconstruction still defaults to the deterministic fake provider, while direct cloud providers remain optional and non-default.

The calling host owns model selection, subscription or API token use, and every associated charge. VibeCAD does not provide a hidden model, Sampling backend, or BYOK billing service.

## Host installation

The repository's canonical skill directory can be copied to a host-specific discovery path. The currently tested Codex installer target is `$CODEX_HOME/skills/vibecad-agent`, with `$HOME/.codex/skills/vibecad-agent` as the default when `$CODEX_HOME` is unset.

Codex also has published discovery paths at `$HOME/.agents/skills/vibecad-agent` for a user and `.agents/skills/vibecad-agent` for a repository. Claude uses `$HOME/.claude/skills/vibecad-agent` for a user and `.claude/skills/vibecad-agent` for a repository. WorkBuddy uses `.codebuddy/skills/vibecad-agent` at project scope.

For WorkBuddy, register the released `vibecad` executable as a local stdio MCP
server using its absolute path, approve that project-scoped server, and restart
or resume the task after runtime readiness. WorkBuddy's native
`ReadMcpResource` persists binary MCP Blob results and returns the saved path;
consume the returned PDF/ZIP resource URI normally and do not invent an
arbitrary-filesystem fallback. When the host supports an allowed-tool list,
exclude runtime maintenance tools from an autonomous CAD task. WorkBuddy may
defer a large MCP tool surface behind `ToolSearch` and `DeferExecuteTool`; in a
headless run, grant those two host tools and only the exact VibeCAD operations
needed by the task. Do not disable permission checks or grant the complete MCP
surface merely to make deferred discovery work.

For WorkBuddy 5.3.5, keep project, task, read, review, ResourceLink, and
`resources/read` operations on MCP, but submit a large handwritten ModelProgram
through the released file adapter so strict contract failures are not collapsed
to a generic `-32603`. Write one project-local file named
`.vibecad-workbuddy-request.json` or
`.vibecad-workbuddy-request-<name>.json` with exactly
`schema_version`, `task_id`, `expected_generation`, and `program`; `program` is
the complete ModelProgram object, not an escaped string. Then invoke only the
released executable's bounded command:

```text
vibecad --workbuddy-submit .vibecad-workbuddy-request-<name>.json
```

Use the absolute released executable path in an actual permission rule. The
adapter accepts only a bounded, owned, non-symlink project-local request file,
reuses the canonical ModelProgram and ParametricDesignIR validators, and returns
the exact error path or a compact persisted task summary. It cannot bypass the
Task Kernel: the Kernel revalidates and executes the same program. If preflight
returns `ok:false`, correct only the named path and submit the current task
generation again; never turn this into an unbounded repair loop. Do not use the
adapter to read artifacts, images, credentials, or arbitrary paths, and do not
grant a general shell merely because this one bounded command is needed.

Installing the MCPB server does not perform skill activation. Copy or link this skill into the chosen host path, then restart or reload the host so it can rediscover the skill; no package channel silently activates it.
