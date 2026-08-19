---
name: vibecad-agent
description: Use VibeCAD's Agent-first MCP surface to turn text or host-visible image references into verified FreeCAD projects and tasks, then inspect, modify, review, and export FCStd/STEP artifacts. Use for bounded CAD work that must remain recoverable and auditable without executing arbitrary Python or FreeCAD code; image understanding stays with the calling multimodal host.
---

# VibeCAD Agent

Use the current 39-tool Agent-first surface. Treat VibeCAD's persisted project, task, revision, draft, visual reconstruction, evidence, artifact, and release records as authoritative. Keep model reasoning, image understanding, subscription, and credentials with the calling host. Never infer success from prose alone.

## Public tools

Runtime and capability tools: `ping`, `get_runtime_status`, `ensure_runtime`, `uninstall_runtime`, `get_capabilities`, `query_freecad_runtime_capabilities`.

Project, task, and delivery tools: `create_project`, `get_project`, `list_projects`, `list_revisions`, `compare_revisions`, `revert_project`, `create_task`, `list_tasks`, `get_task`, `get_task_events`, `submit_model_program`, `resume_task`, `cancel_task`, `accept_draft`, `reject_draft`, `get_artifact_manifest`, `export_task_artifacts`, `create_release`, `get_release`, `approve_release`.

Visual reconstruction tools: `create_reconstruction`, `get_reconstruction`, `run_reconstruction`, `answer_reconstruction`, `adopt_reconstruction`, `reject_reconstruction`, `delete_reconstruction`.

Direct CAD tools: `create_box`, `create_cylinder`, `inspect_model`, `modify_parameter`, `move_part`, `rotate_part`.

Use a direct tool for one supported operation with explicit inputs. Use ModelProgram for an ordered multi-command change. Both direct and ModelProgram paths enter the same Task Kernel, so recovery, verification, review, and acceptance semantics stay identical.

Project, task, revision, review, artifact, release, and CAD MCP calls plus the public Workbench client use one same-user authenticated local daemon and shared Task Kernel. Runtime maintenance and inert discovery remain local MCP server concerns. FreeCAD runs behind the kernel in a managed, killable Worker generation. The G1 FreeCAD Workbench alpha uses this same authority for review and Release actions.

## Required workflow

Initialize or verify the runtime first. Once it is ready, call `get_capabilities` as the first business discovery tool instead of guessing CAD support or arguments. Keep every write attached to the returned project id, task id, generation, base revision, draft revision, and idempotency key.

Use `query_freecad_runtime_capabilities` only when the host needs the exact native TypeId inventory of the active managed FreeCAD build. Page with the returned opaque cursor and repeat the same `module`, `semantic_kind`, `minimum_status`, and `limit`; discard the cursor and restart at page one if an integrity error reports cursor drift. A `discovered` entry is inventory, not execution authority. Use `get_capabilities` for the stable executable Agent operations and never infer that an arbitrary discovered TypeId is callable.

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

Limit the verified public V1 envelope to one dimension-complete mechanical extruded or revolved
part. Accept either one fully dimensioned view or two to sixteen clean complementary views. Use
editable Sketcher geometry and only the bounded PartDesign feature chain described in the
parametric reference. Do not present this as an arbitrary-photo or reverse-engineering promise.
Before CAD creation, build a compact evidence
matrix whose rows are the dimensions and feature relationships that affect geometry and whose
columns name the exact source index and view role. Mark a fact `cross_view_derived` only when it
uses at least two distinct known view roles from the same object, state, and scale. Do not treat
duplicate views, detail crops of one source, or two unknown-role images as cross-view evidence
merely because there is more than one file.

Stop before `create_task` when two views disagree about the same dimension, when the available
views share a silhouette but leave extrusion depth unresolved, or when a hidden feature changes
the model without direct evidence. Ask one bounded clarification that names the conflicting or
missing fact. Do not average conflicting dimensions, choose the more convenient view, estimate a
depth from perspective, or submit alternative guesses as confirmed geometry.

For a multi-location `hole`, group only circles on the same sketch plane that share one diameter,
extent/depth, and direction; list every circle identity in `location_geometry_ids`. Rely on the
compiler's separate material-removal proof for every declared hole axis, and put at most 16
locations in one Hole feature. Use multiple sequential single-loop Pocket features when needed; do not submit a
single multi-loop Pocket. Keep separate planes, diameters, depths, or directions as separate
sketches and linear features.

When an explicitly evidenced generated edge needs a Fillet or Chamfer, author the optional
`edge_treatments` tail from the parametric reference. Select only by source feature + source sketch
geometry + `section_start`/`section_end`/`sweep` role; never write `EdgeN` or reuse a GUI edge
label. Fillet supports a constant radius, independent per-edge radii, or one linear start-to-end
radius law on an oriented nonclosed edge. Chamfer is symmetric per edge. Treat any ambiguous,
missing, direction-flipped, or kernel-invalid resolution as a safe failure; do not substitute an
imported STEP edge or an inferred tangent chain.

Never infer an absolute dimension from an unscaled photo. Ask only for missing facts that block a safe parameterized model. Once the required dimensions and feature relationships are sufficient, call `get_capabilities`, create or select the project, call `create_task` with `require_review`, and submit the bounded construction through `submit_model_program`. Continue through the persisted task `next_action`, deterministic verification, draft review, and artifact workflow exactly as for a text request.

Before authoring a `create_parametric_design` command, read `references/parametric-design-ir-v1.md`. `get_capabilities` exposes the operation and its `parametric_design_ir` value shape but not the nested wire contract; the reference is the portable host-side authoring contract. Do not improvise omitted fields, enum values, identity formats, feature order, or evidence states.

The CAD task and ModelProgram become durable after submission; the host's original attachment does not automatically become VibeCAD durable evidence. If a host restart loses image or clarification context, ask the user to reattach or restate the missing evidence. Never reconstruct dimensions from a task id alone.

## Guided ordinary photos

For ordinary physical-part photos, read `references/guided-photo-v1.md` before deciding whether
to create CAD. Keep the object inside the single rigid extruded/revolved mechanical envelope and
classify the capture as `PHOTO_READY`, `NEEDS_CAPTURE`, or `OUT_OF_ENVELOPE`. Require direct user
measurements or a scale reference on the same physical plane as the measured boundary; do not
apply one pixel ratio across perspective depth.

Check background separation, blur/glare, silhouette completeness, view roles, occlusion, and the
geometry facts needed for the bounded ParametricDesignIR. Ask for one named recapture or
measurement at a time. When hidden geometry has multiple plausible branches, present the bounded
alternatives for confirmation, then discard the provisional plan and rebuild the evidence matrix
from the original sources plus the user's answer. Stop before `create_task` unless every
geometry-changing fact is confirmed and the capture is `PHOTO_READY`.

## Optional sealed-image visual reconstruction

Use the seven visual-reconstruction tools only when an explicitly selected VibeCAD-managed workflow needs a durable local ImageSet or optional Provider lifecycle. A trusted local host adapter must first seal one to sixteen JPEG/PNG inputs and return the exact `image_set_id` and `image_set_manifest_sha256`. WorkBuddy direct attachment ingress into that sealed store is not verified and is not required for host-owned image-to-CAD. Never put a path, Base64 value, image bytes, filename, or Resource URI into a reconstruction MCP request, and never invent or alter either ImageSet binding.

Create the draft with `create_reconstruction`, one retained key matching `reconstruction_create_[0-9a-f]{32}`, the target project, and the exact sealed ImageSet binding. Replay an unknown create outcome only with that identical request. Use `get_reconstruction` to recover the current generation and route only its persisted `next_action`: call `run_reconstruction` for `run`, answer the named bounded question with `answer_reconstruction` for `answer`, and present the proposal summary to the user for `adopt_or_reject`. Pass the exact observed generation to every mutation. A run must provide both its budget and deadline or set both to null.

When a successful reconstruction result includes `review_resources`, consume the accompanying
standard `ResourceLink` entries with `resources/read`. Each resource is an immutable local PNG
bound to the exact observation, source index, byte size, and SHA-256. Verify those fields before
displaying it. The overlay is `advisory_only`: it helps the user review landmarks, fitted
primitives, and uncertainty, but it cannot accept a CAD draft, change Task/Revision/HEAD, or turn
an uncalibrated image into dimensional truth. An empty list is valid before review evidence has
been rendered; never fabricate a URI or fall back to an arbitrary local path.

Adoption is eligible only when VibeCAD's internal exact-evidence admission recomputes as `COMPLETE`
from the sealed images, provider evidence, calibration facts, fitted geometry, proposal, and user
answers. The caller cannot supply or narrow that admission. Missing, stale, ambiguous, or tampered
input fails closed before Task creation; inspect the reconstruction and recreate the affected input
instead of blindly retrying `adopt_reconstruction`.

Call `adopt_reconstruction` only after the user chooses the displayed proposal. Adoption creates an ordinary `REQUIRE_REVIEW` CAD Task; it does not accept a draft or advance project HEAD. Continue that returned task through the normal task/review workflow. Use `reject_reconstruction` to retain a rejected record, or `delete_reconstruction` only when the user wants the draft and its bound local image source removed. Do not treat the deterministic default provider as real photo-to-CAD inference, and do not make this optional lifecycle the default when the host already sees the images.

## Artifact delivery

Call `get_artifact_manifest` first with the exact task generation, revision, and nullable draft binding. If it returns `materialized=true`, consume its typed `ResourceLink` entries and call `resources/read` for their URIs. If it returns `materialized=false`, call `export_task_artifacts` once with a retained export key, then consume its returned `ResourceLink` entries through `resources/read`. The manifest query is read-only: never expect it to create, copy, validate, or repair a delivery. Verify format, byte size, and SHA-256 evidence before handing off the FCStd and STEP files.

Never request, expose, or read an arbitrary filesystem path. Artifact access must use the verified resource URI returned by VibeCAD.

## Release delivery

Create a Release only from a `succeeded` task's exact committed Revision and observed task generation. Call `create_release` with one retained `release_create_[0-9a-f]{32}` key, then inspect the returned drawing, BOM JSON/CSV, manifest, validation report, Revision digest, verification digest, and package digest. A draft exposes preview `ResourceLink` entries but never exposes the ZIP resource URI.

Approval is a separate user decision. After the user approves the exact displayed package SHA-256, call `approve_release` with the draft generation, that unchanged digest, and one retained `release_approve_[0-9a-f]{32}` key. Only an approved Release exposes `vibecad-release.zip`; retrieve it with `resources/read`. Re-read with `get_release` after restart or an unknown response, and replay only the identical idempotency key and expected digest. Release approval never changes Revision or project HEAD.

The current buffered Release resource ceiling is 64 MiB. If creation returns `resource_exhausted`, report that transport limit; do not bypass it with an arbitrary filesystem path or claim that a larger package was approved.

Never reconstruct retired tool names. Use only the live 39-tool surface above.

Never generate or execute arbitrary Python/FreeCAD code. FreeCAD is the bounded geometry engine behind VibeCAD, not an authorization to run model-generated code.

## Unsupported and unavailable capabilities

STEP and STL import unavailable in the verified current envelope; only FCStd Box/Cylinder import is supported. Do not claim `mcp_sampling`, `byok`, Workbench UI, `face/edge` selection, STL reconstruction, universal or release-verified photo reconstruction, or simulation. Host-owned image reasoning is limited to the dimension-complete single-part mechanical envelope when the calling multimodal host can actually see the attachments; guided scale-backed photos must pass the Guided Photo contract, and ordinary unscaled photos remain outside the envelope. VibeCAD-managed visual reconstruction still defaults to the deterministic fake provider, while direct cloud providers remain optional and non-default.

The calling host owns model selection, subscription or API token use, and every associated charge. VibeCAD does not provide a hidden model, Sampling backend, or BYOK billing service.

## Host installation

The repository's canonical skill directory can be copied to a host-specific discovery path. The currently tested Codex installer target is `$CODEX_HOME/skills/vibecad-agent`, with `$HOME/.codex/skills/vibecad-agent` as the default when `$CODEX_HOME` is unset.

Codex also has published discovery paths at `$HOME/.agents/skills/vibecad-agent` for a user and `.agents/skills/vibecad-agent` for a repository. Claude uses `$HOME/.claude/skills/vibecad-agent` for a user and `.claude/skills/vibecad-agent` for a repository. WorkBuddy uses `.codebuddy/skills/vibecad-agent` at project scope.

For WorkBuddy, register the released `vibecad` executable as a local stdio MCP
server using its absolute path, approve that project-scoped server, and restart
or resume the task after runtime readiness. WorkBuddy's native
`ReadMcpResource` persists binary MCP Blob results and returns the saved path;
consume returned PNG/PDF/ZIP resource URIs normally and do not invent an
arbitrary-filesystem fallback. When the host supports an allowed-tool list,
exclude runtime maintenance tools from an autonomous CAD task. WorkBuddy may
defer a large MCP tool surface behind `ToolSearch` and `DeferExecuteTool`; in a
headless run, grant those two host tools and only the exact VibeCAD operations
needed by the task. Do not disable permission checks or grant the complete MCP
surface merely to make deferred discovery work.

For WorkBuddy 5.3.5, keep project, task, read, review, ResourceLink, and
`resources/read` operations on MCP, but submit a large handwritten ModelProgram
through the released file adapter so strict contract failures are not collapsed
to a generic `-32603`. This WorkBuddy-specific rule overrides the generic
`submit_model_program` instruction above: do not try MCP submission first and do
not fall back to it after writing the request file. Write one project-local file named
`.vibecad-workbuddy-request.json` or
`.vibecad-workbuddy-request-<name>.json` with exactly
`schema_version`, `task_id`, `expected_generation`, and `program`; `program` is
the complete ModelProgram object, not an escaped string. The file root is never
the bare ModelProgram. Before writing, validate that each profile uses an
independent constraint set, every non-Revolve feature has `axis: null`, and a
Hole sketch's diameter constraints reuse the same parameter identity as the
Hole feature's `diameter`; do not introduce a separate radius parameter. Then
invoke only the released executable's bounded command:

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
