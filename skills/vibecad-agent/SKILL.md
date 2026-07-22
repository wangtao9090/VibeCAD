---
name: vibecad-agent
description: Use VibeCAD's Agent-first MCP surface to create, inspect, modify, review, and export verified FreeCAD projects and tasks. Use for bounded CAD work that must remain recoverable, auditable, and deliver FCStd/STEP artifacts without executing arbitrary Python or FreeCAD code.
---

# VibeCAD Agent

Use the current 20-tool Agent-first surface. Treat VibeCAD's persisted project, task, revision, draft, evidence, and artifact records as authoritative. Never infer success from prose alone.

## Public tools

Runtime and capability tools: `ping`, `get_runtime_status`, `ensure_runtime`, `uninstall_runtime`, `get_capabilities`.

Project and task control tools: `create_project`, `get_project`, `create_task`, `get_task`, `submit_model_program`, `resume_task`, `accept_draft`, `reject_draft`, `export_task_artifacts`.

Direct CAD tools: `create_box`, `create_cylinder`, `inspect_model`, `modify_parameter`, `move_part`, `rotate_part`.

Use a direct tool for one supported operation with explicit inputs. Use ModelProgram for an ordered multi-command change. Both direct and ModelProgram paths enter the same Task Kernel, so recovery, verification, review, and acceptance semantics stay identical.

## Required workflow

Initialize or verify the runtime first. Once it is ready, call `get_capabilities` as the first business discovery tool instead of guessing CAD support or arguments. Keep every write attached to the returned project id, task id, generation, base revision, draft revision, and idempotency key.

```text
get_runtime_status
  -> ensure_runtime only when the runtime is not ready
  -> get_capabilities
  -> create_project
  -> create_task
  -> get_task
  -> route the persisted next_action until review or terminal state
  -> export_task_artifacts
  -> resources/read for each returned resource URI
```

Before a mutating call, read the current task when state may have advanced. After a mutating call, use the returned state and generation; do not replay merely because a response is slow. Accept only the named draft based on its evidence, or reject it with an explicit reason.

`create_project` supports `empty` or `import_fcstd`; the verified `import_fcstd` envelope accepts only a nonempty FCStd whose objects are all `Part::Box` or `Part::Cylinder`, and must reject every unsupported or mixed object type.

### Route `next_action`

| Persisted action | Required behavior |
|---|---|
| `request_plan` | Call `get_task` once; if the action remains, stop and report an internal-state mismatch. |
| `submit_program` | Submit the prepared bounded program with `submit_model_program`, or use the matching direct operation when the task contract permits it. |
| `provide_input` | Supply the requested bounded input through `submit_model_program`, or use the matching direct operation when the task contract permits it. |
| `validate_program` | Continue the persisted transition with `resume_task`. |
| `reconcile` | Continue the persisted transition with `resume_task`. |
| `cleanup` | Continue the persisted transition with `resume_task`. |
| `review_draft` | Inspect the evidence, then call either `accept_draft` or `reject_draft`. |
| `wait` | Poll with `get_task`; if the persisted state is resumable, call `resume_task` at most once for that observed generation. |
| `none` | Stop mutation and report the terminal or non-actionable state. |

If the outcome of `create_task` is unknown and no task id or task_id was received, stop and report the bounded orphan risk; never retry that creation call. A later recovery API may make the orphan discoverable, but this skill must not invent an id.

## Artifact delivery

After successful `export_task_artifacts`, consume the returned typed `ResourceLink` entries and call `resources/read` for their URIs. Verify format, byte size, and hash or sha256 evidence before handing off the FCStd and STEP files.

Never request, expose, or read an arbitrary filesystem path. Artifact access must use the verified resource URI returned by VibeCAD.

Never call a legacy 31-tool surface or reconstruct retired tool names. Use only the live 20-tool surface above.

Never generate or execute arbitrary Python/FreeCAD code. FreeCAD is the bounded geometry engine behind VibeCAD, not an authorization to run model-generated code.

## Unsupported and unavailable capabilities

STEP and STL import unavailable in the verified current envelope; only FCStd Box/Cylinder import is supported. Do not claim `mcp_sampling`, `byok`, Workbench UI, `face/edge` selection, STL reconstruction, photo reconstruction, or simulation. Route those needs to later product stages or an explicitly approved external engine.

The calling host owns model selection, subscription or API token use, and every associated charge. VibeCAD does not provide a hidden model, Sampling backend, or BYOK billing service.

## Host installation

The repository's canonical skill directory can be copied to a host-specific discovery path. The currently tested Codex installer target is `$CODEX_HOME/skills/vibecad-agent`, with `$HOME/.codex/skills/vibecad-agent` as the default when `$CODEX_HOME` is unset.

Codex also has published discovery paths at `$HOME/.agents/skills/vibecad-agent` for a user and `.agents/skills/vibecad-agent` for a repository. Claude uses `$HOME/.claude/skills/vibecad-agent` for a user and `.claude/skills/vibecad-agent` for a repository.

Installing the MCPB server does not perform skill activation. Copy or link this skill into the chosen host path, then restart or reload the host so it can rediscover the skill; no package channel silently activates it.
