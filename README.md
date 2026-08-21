# VibeCAD

**[English](README.md)** | [简体中文](README.zh-CN.md)

[![CI](https://github.com/wangtao9090/VibeCAD/actions/workflows/ci.yml/badge.svg)](https://github.com/wangtao9090/VibeCAD/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.12+](https://img.shields.io/badge/python-3.12+-blue.svg)](https://www.python.org)

VibeCAD is a FreeCAD expert agent for host agents such as Claude, Codex, and WorkBuddy. It turns design
intent into persistent projects, constrained CAD operations, reviewable drafts, and verified
FCStd/STEP resources.

VibeCAD neither embeds nor resells a large language model. Reasoning uses the user's own host
model and its subscription or API quota; VibeCAD is responsible for CAD contracts, isolated
execution, deterministic verification, recovery, and delivery.

## What VibeCAD Delivers

- An Agent-native path from design intent to persistent CAD projects and versioned results.
- A real FreeCAD Workbench Alpha for project/task discovery, HEAD and draft preview, verdict,
  Accept, and Reject.
- Deterministic Task Kernel execution: isolated candidates, explicit review policy, verified
  FCStd/STEP artifacts, recovery, and replay-safe request semantics.
- Editable parametric designs with bounded derived-parameter linkage and semantic Fillet/Chamfer
  treatments, including one linear start-to-end variable-radius fillet on an oriented edge.
- A host-neutral 39-tool MCP and Skill contract for Codex, Claude, WorkBuddy, and other compatible
  agents; each real host profile is certified separately against the same package smoke.
- Additional WorkBuddy 5.3.5 compatibility coverage for strict-error recovery, durable restart,
  exact Release approval, and native MCP Blob reads for PDF/ZIP delivery.
- A VibeCAD-managed FreeCAD runtime, so users do not need to prepare a compatible system FreeCAD.

## Try the FreeCAD Workbench Alpha

The easiest installation path is to give your coding Agent this request:

> Install and launch the VibeCAD FreeCAD Workbench Alpha from
> https://github.com/wangtao9090/VibeCAD. Use tag `v0.10.0`, clone it into a persistent
> directory, build its wheel, install it with `uv tool install --force`, keep
> the checkout and built wheel, and run `vibecad --freecad`. Do not install or
> fall back to a system copy of FreeCAD.

The Agent's reproducible procedure is:

```bash
git clone https://github.com/wangtao9090/VibeCAD.git VibeCAD
git -C VibeCAD checkout v0.10.0
cd VibeCAD
uv build --wheel
uv tool install --force dist/vibecad-0.10.0-py3-none-any.whl
vibecad --freecad
```

Installation notes:

- Keep the persistent checkout and built wheel at the same path while this Alpha is installed.
- Do not search `PATH`, `/Applications`, the normal FreeCAD `Mod` directory, or install a system
  FreeCAD fallback. `vibecad --freecad` owns the verified managed runtime.
- Allow the first launch to download approximately 2–3 GB of locked runtime files; later launches
  reuse them.
- Success means managed FreeCAD opens with the VibeCAD Workbench and review Dock active. On
  failure, report the exact launcher error and stop instead of switching runtimes or inventing an
  alternate installation path.

The Dock can list projects and tasks, refresh selected state, open separate managed HEAD and
draft preview documents, show the review verdict, capture exact whole-object or feature
`SelectorV1` values, and Accept or Reject a fresh draft. Face/edge subelement selection is not
claimed.

The current P1 source also provides a sequential manual-finish path after Agent review ends:
**Open Editable HEAD** creates a non-authoritative working copy, normal **Save** stays local,
**Checkpoint Edit** verifies and publishes a new Revision, and **Discard Edit** publishes nothing.
Agent preview and editable HEAD are mutually exclusive; there is no automatic merge or rebase.

The managed launcher above remains the default and fallback. One additional, deliberately narrow
macOS pilot can install the same thin Workbench into an explicitly selected user FreeCAD:

```bash
vibecad --freecad-app /Applications/FreeCAD.app --doctor
vibecad --freecad-app /Applications/FreeCAD.app --install-addon
# reversible cleanup
vibecad --freecad-app /Applications/FreeCAD.app --uninstall-addon
```

This is not general system-FreeCAD support. The current local evidence admits only the exact
fingerprinted macOS FreeCAD 1.1.3 host with embedded CPython 3.11 and PySide6 6.8.3. The doctor
fails closed for every other host. The installed addon holds no daemon secret and delegates
selector construction and unique resolution to the managed Python bridge and the same Task
Kernel used by managed mode.

## Current Agent-first Workflow

```text
User text or images and the host multimodal Agent
  → host classifies visible facts as confirmed, inferred, or unknown
  → host asks for blocking dimensions instead of inventing absolute scale
  → get_capabilities reads the actual capabilities
  → create_project creates an empty project or performs a controlled FCStd import
  → create_task binds the project version and review policy
  → call one direct operation, or submit a multi-step ModelProgram
  → Task Kernel executes and verifies the candidate version in an isolated checkout
  → auto_commit publishes, or require_review waits for Accept/Reject
  → export_task_artifacts returns FCStd/STEP ResourceLinks
  → resources/read reads and verifies the delivered resources
```

Direct operations and ModelPrograms are not separate execution systems. A direct operation
simply compiles one explicit operation into a single-command ModelProgram. Both paths enter the
same Task Kernel and share the same project lease, immutable base revision, candidate checkout,
verification, draft, commit, reject, rollback, and recovery semantics.

A project can currently begin only from an empty project or a single FCStd file. An FCStd import
must be non-empty, and every object in it must be either `Part::Box` or `Part::Cylinder`. Mixed
or other object types are rejected. General FCStd import belongs to P1; STEP/STL import, reverse
engineering, and simulation are not yet integrated. For an image request, the calling Codex,
Claude, WorkBuddy, or other multimodal host performs image understanding with its own subscription
or API authorization, then submits the resulting bounded ModelProgram through the ordinary Task
Kernel. VibeCAD does not need the host's model credential or upload the same image to a second model.
The current bounded image-to-CAD alpha covers one mechanical extruded or revolved part. It accepts
a dimension-complete view, two to sixteen clean complementary views, or ordinary photos that pass
the guided capture, scale, occlusion, and geometry-completeness gates and have the blocking dimensions
confirmed independently. The result is editable Sketcher/PartDesign output. Public outcome evidence
includes a single-hole plate, a sharp-shoulder stepped shaft, a three-view L bracket, an annular
washer, a rounded-square fan spacer, and a calibration block with one blind pocket. Missing scale or
depth, contradictory dimensions, multiple objects, material occlusion, or hidden structure must ask
for one bounded recapture/measurement or stop before Task creation. The canonical Agent skill carries
portable `ParametricDesignIR v1` and `Guided Photo v1` references; this is not photo-only metrology or
a claim of arbitrary reverse engineering.

## Current Public Capabilities (development branch)

The MCPB manifest and runtime project the same frozen contract, which currently exposes 39
tools. Each tool has a concise description, a strict input schema, and side-effect annotations.
A host should call `get_capabilities` first instead of inferring capabilities from the number of
tools or from general model knowledge.

| Category | Tools |
|---|---|
| Service and runtime | `ping`, `get_runtime_status`, `ensure_runtime`, `uninstall_runtime` |
| Capability discovery | `get_capabilities`, `query_freecad_runtime_capabilities` |
| Projects and versions | `create_project`, `get_project`, `list_projects`, `list_revisions`, `compare_revisions`, `revert_project` |
| Tasks and drafts | `create_task`, `list_tasks`, `get_task`, `get_task_events`, `submit_model_program`, `resume_task`, `cancel_task`, `accept_draft`, `reject_draft` |
| Delivery | `get_artifact_manifest`, `export_task_artifacts`, `create_release`, `get_release`, `approve_release` |
| Visual reconstruction | `create_reconstruction`, `get_reconstruction`, `run_reconstruction`, `answer_reconstruction`, `adopt_reconstruction`, `reject_reconstruction`, `delete_reconstruction` |
| Direct operations | `create_box`, `create_cylinder`, `inspect_model`, `modify_parameter`, `move_part`, `rotate_part` |

The seven visual-reconstruction tools are an optional VibeCAD-managed lifecycle for sealed local
ImageSets, durable restart recovery, clarification, and adoption into an ordinary reviewed CAD
Task. The default composition remains deterministic fake. A non-MCP local host adapter can seal
one to sixteen JPEG/PNG inputs through one authenticated staging-directory descriptor; the JSON
wire contains no path, filename, base64 payload, or image bytes. This optional store/provider path
is not required when the calling multimodal host already sees the images. Direct WorkBuddy
attachment ingress into VibeCAD's sealed store remains unverified, and the MCP surface accepts no
image path, base64 payload, or visual Resource URI.

`get_capabilities` remains the stable executable Agent-operation contract. The read-only
`query_freecad_runtime_capabilities` tool pages through the content-bound native TypeId inventory
of the active managed FreeCAD build; a `discovered` entry is inventory and never grants execution
authority. Its opaque cursor is bound to the runtime, filters, and page size and fails closed after
any drift.

A successful `export_task_artifacts` call returns a canonical result and two typed
`ResourceLink` values:

- FCStd: `application/vnd.freecad.fcstd`;
- STEP: `model/step`.

The host can retrieve binary content only by calling `resources/read` with the returned URI, then
checking its format, size, and SHA-256. The interface does not provide arbitrary-path export or
arbitrary file reads.

For an accepted Revision, `create_release` generates a previewable A3 assembly PDF, flat BOM,
manifest, validation report, and an immutable seven-file delivery ZIP. The host must present the
exact ZIP SHA-256 before calling `approve_release`; only the approved Release exposes the ZIP
ResourceLink. Release approval is separate from Revision acceptance and never changes project
HEAD.

## Why the Model Does Not Execute FreeCAD Python Directly

FreeCAD is the geometry engine and execution environment, but “the code runs” does not mean “the
design matches the intent.” The primary path accepts only versioned ModelPrograms with a bounded
operation set and bounded budgets. It does not accept arbitrary Python/FreeCAD code generated by
the model, nor does it use such code as a fallback channel after failure.

The Task Kernel provides the following guarantees for every write:

- Inputs pass strict schema, selector, budget, and AcceptanceSpec validation.
- Execution occurs in an isolated candidate copy rather than modifying the user's source file in place.
- Results are bound to the base revision, task generation, verification evidence, and an immutable revision.
- `auto_commit` publishes only after verification succeeds and HEAD has not drifted.
- `require_review` creates a persistent draft; Accept publishes it, while Reject leaves HEAD unchanged.
- Delivery state, provenance, hashes, and sizes are verified again during export and read.

Before calling `create_task`, the host must generate and persist a `task_create_` request key. If
the response outcome is unknown, replay `create_task` with exactly the same key, project, and
review policy. The Task Kernel returns the current generation of the same task rather than
creating a second task.

The first `cancel_task` call must use the task generation that was just read. For an idle task in
`created`, `needs_plan`, `program_ready`, or `needs_input`, it immediately persists `cancelled`
with CAS. If the cancellation response is unknown, the exact same request can be replayed to
obtain the same cancellation result. A running task persists its cancellation state. When the
current task returns `next_action=reconcile`, the host must first read the task, then call
`resume_task` once with the generation just returned. It must not guess whether the Worker has
stopped or fabricate a future generation. A draft awaiting review must be handled with
`reject_draft`.

Idle cancellation changes only the task record. It does not start CAD/runtime, construct
artifact/export components, acquire the project write lease, or modify project HEAD, source
files, or the delivery directory. MCP `notifications/cancelled` cancels only one transport
request; it is not persistent task cancellation.

Use paginated `list_projects` discovery only when the project id is unknown, then call
`get_project` to read the current authoritative HEAD. `list_revisions` returns only committed
ancestors of that project's current HEAD. Results are sorted by canonical revision id rather
than by time; reconstruct the commit chain from the returned `head` by following
`base_revision`. Draft, candidate, and abandoned revisions are not returned as committed
history. If any paginated cursor returns `conflict`, discard the cursor and restart from the
first page.

`compare_revisions` revalidates the manifests and actual FCStd/STEP files of two committed
revisions. It reports only lineage, file presence, and differences in identity, SHA-256, and
size; semantic diffs for geometry, solids, and parameters are explicitly `unsupported`. Before
delivery, call the read-only `get_artifact_manifest`. If a verified PUBLISHED delivery already
exists, it directly returns two ResourceLinks. Otherwise it returns `materialized=false`
without creating, copying, or cleaning any delivery file; only then should
`export_task_artifacts` be called.

## Installation: The MCP Service and Agent Skill Are Separate

The current MCPB product declaration covers the verified macOS (Darwin) and Windows x86-64
paths. Installing `VibeCAD.mcpb` installs the MCP service, but the bundled Skill is archive
content and is not activated automatically. The host must separately copy or link
`skills/vibecad-agent/`, then restart or reload the host. Linux and Windows on ARM are not part
of this support declaration.

Skill discovery paths are:

| Host | User-level path | Project-level path |
|---|---|---|
| Current Codex installer path | `$CODEX_HOME/skills/vibecad-agent`; defaults to `$HOME/.codex/skills/vibecad-agent` when unset | — |
| Published Codex discovery path | `$HOME/.agents/skills/vibecad-agent` | `.agents/skills/vibecad-agent` |
| Claude Code | `$HOME/.claude/skills/vibecad-agent` | `.claude/skills/vibecad-agent` |
| WorkBuddy | — | `.codebuddy/skills/vibecad-agent` |

The release asset `vibecad-agent-skill-0.10.0.zip` contains exactly one top-level
`vibecad-agent/` directory after extraction. That directory can be copied as a whole to any path
listed above. The Python wheel contains the server and the FreeCAD Workbench addon, while the
managed runtime contains the matching server environment. Neither package activates the Agent
Skill.

### WorkBuddy (verified)

Install the released CLI, copy the standalone Skill directory to
`.codebuddy/skills/vibecad-agent`, and register the local stdio server in the
project's `.mcp.json`. Use the absolute path returned by `command -v vibecad`
for `command` so the GUI does not depend on an inherited shell `PATH`:

```json
{
  "mcpServers": {
    "vibecad": {
      "command": "/absolute/path/to/vibecad",
      "args": [],
      "env": {"VIBECAD_AUTO_INSTALL": "1"}
    }
  }
}
```

Approve that project-scoped server when WorkBuddy prompts, then start or resume
the task after the runtime reports ready. WorkBuddy natively persists binary
`resources/read` results into its project `.mcp-resources/` directory, so PDF
and approved ZIP delivery need no filesystem adapter. GLM-5.2 passed the
canonical multi-turn task, but remains a provisional default: keep runtime
maintenance tools outside an autonomous CAD task's allowed-tool set and require
explicit user confirmation for `uninstall_runtime`.

For the current 39-tool visual development profile, WorkBuddy 5.3.5 should
submit a handwritten ModelProgram through the bounded project-local command
`vibecad --workbuddy-submit .vibecad-workbuddy-request-<name>.json`; the
canonical Skill defines the exact four-field request. This preserves actionable
contract paths that WorkBuddy may otherwise surface only as `-32603`. It is not
an artifact adapter or second execution path: ResourceLink/Blob delivery stays
native MCP, and the existing Task Kernel revalidates and executes the program.

The Windows host path is also verified with WorkBuddy 5.3.13 / CLI 2.115.0:
one real task reached committed HEAD, all deterministic box checks passed, and
native FCStd/STEP resource reads matched the VibeCAD manifest byte-for-byte.
For that exact host version, use `"draft_id": "committed"` when selecting
committed artifacts; VibeCAD normalizes this documented alias to the canonical
`null` scope without changing draft or authority semantics.

On first launch, the extension needs network access to fetch locked Python packages and, when
needed, install approximately 2–3 GB of FreeCAD runtime files. Later launches reuse the verified
cache. The default data root is platform-specific:

```text
macOS:  ~/Library/Application Support/VibeCAD/
Windows: %LOCALAPPDATA%\VibeCAD\
```

Runtime and project data are separate. `uninstall_runtime` first presents a preview and then
requires explicit confirmation. It deletes only the managed runtime while preserving project,
revision, draft, and artifact data. The host settings can then remove the extension itself.

### Local Development

```bash
uv sync --frozen
PYTHONPATH=src uv run --frozen pytest
uv run --frozen ruff check .
VIBECAD_AUTO_INSTALL=0 uv run --frozen python -m vibecad.server
```

FreeCAD is not a normal Python dependency; the runtime installer manages it separately. Real
runtime integration tests must be enabled explicitly:

```bash
VIBECAD_RUN_INTEGRATION=1 PYTHONPATH=src uv run --frozen pytest -m slow
```

## What “Host-ready” Means Precisely

The 0.10.0 release contract verifies the MCP protocol, Skill package structure, FCStd/STEP and
Release ResourceLinks, managed FreeCAD E2E, and exact 39-tool discovery independently of any host.
Real Codex, Claude, and WorkBuddy package smokes are recorded as separate host profiles; passing one
never certifies the others. WorkBuddy additionally carries the compatibility coverage described
above, and no result claims that every model available in a host is certified.

## Architectural Boundaries and Roadmap

The current domain path is MCP transport/server → same-user authenticated local daemon → single
Agent application → Task Kernel → CAD execution port → managed killable FreeCAD Worker. The
public Workbench client likewise enters the Application/Task Kernel through the daemon. Runtime
maintenance and stateless discovery remain local responsibilities of the MCP server and do not
form a second domain-write path. The daemon provides same-user authentication and constrained,
one-time file grants; it does not create a second commit system.

S3-8, P0-B core, the package/managed-runtime closeout, bounded G1 Workbench Alpha, P1 sequential
editing, P2 rigid mechanical delivery, bounded visual mechanical CAD, and host integration are
included in 0.10.0:

- **P0-B core (backend complete)**: task/project/version discovery, file-level comparison,
  verified forward revert, cancellation/reconcile, authenticated daemon, file grants, source
  liveness, and the managed killable FreeCAD Worker all enter the same Task Kernel;
- **G1 (Alpha complete)**: preview, verdict, exact object/feature selector capture, and
  Accept/Reject are available in the real FreeCAD Qt Workbench UI; one fingerprinted external
  FreeCAD 1.1.3 pilot is evidenced, while managed mode remains the default;
- **P1/G2 (complete boundary)**: the narrow sequential editable-HEAD/manual-checkpoint slice and
  bounded native Sketcher/PartDesign parameter editing are implemented; controlled general import
  and broader single-part production capability remain;
- **P2 (complete boundary)**: rigid 2–10 component assemblies, interference verification, flat
  BOM, deterministic assembly PDF, immutable Release approval, and an exact delivery ZIP;
  native joints, editable manufacturing drawings, GD&T, PLM, and enterprise delivery chains remain;
- **WorkBuddy (verified)**: WorkBuddy 5.3.5 with GLM-5.2 completed strict local stdio tool use,
  durable task/restart recovery, exact digest approval, and native PDF/ZIP Blob reads; the wider
  model comparison remains future evidence, not a release blocker. In the recorded v0.7.0 Visual
  Mechanical host evidence, GLM-5V-Turbo also turned the frozen dimensioned plate fixture into a
  verified editable draft via the bounded submit adapter; this is not a claim of universal photo
  reconstruction.
- **Visual Mechanical V1**: one fully dimensioned view, or 2–16 clean complementary views of the
  same object/state/scale, can produce one editable extruded or revolved mechanical part. Missing
  scale/depth, conflicting views, occlusion, and hidden structure must clarify or fail safely.
- **Guided Photo V3**: bounded ordinary-photo parts may use capture/scale/completeness screening,
  native editable slots, and independently confirmed critical dimensions before entering the same
  reviewed Task Kernel. Three public positives and two pre-Task safe failures define the claim;
  arbitrary photo metrology, hidden geometry recovery, and freeform remain excluded. Confirmed
  generated edges may use bounded semantic Fillet/Chamfer treatments; the system does not infer
  those treatments from photographs alone.
- **Parametric linkage and edge treatments**: structured affine derived parameters drive native
  FreeCAD expressions, while stable source-feature/sketch-role semantics select generated edges for
  constant, per-edge, or linear variable-radius Fillet and symmetric Chamfer operations. Ambiguity,
  invalid geometry, and direction reversal fail the whole candidate transaction closed.

The G1 Workbench Alpha packages the real FreeCAD Qt UI and its deterministic managed launcher. It
includes one Workbench and Dock, daemon-backed refresh, separate HEAD/draft preview, verdict,
exact object/feature selector capture, Accept/Reject, and asynchronous client/thread shutdown.
The daemon is a reusable managed background service; update and uninstall retire it through the
authenticated maintenance path. The thin external pilot reuses those state machines through one
bounded managed-Python bridge and does not add a second write authority. Face/edge selection,
STEP/STL import, universal photo reconstruction, and simulation are not currently supported. A
multimodal host can pilot bounded image-to-CAD through the ordinary reviewed Task flow; the separate
VibeCAD-managed visual lifecycle still defaults to the deterministic fake Provider, with direct
cloud transport optional and non-default.

Further reading in the source repository:
[User Guide](https://github.com/wangtao9090/VibeCAD/blob/main/docs/USER_GUIDE.md),
[Acceptance Tests](https://github.com/wangtao9090/VibeCAD/blob/main/docs/ACCEPTANCE_TESTS.md),
[Overall Architecture](https://github.com/wangtao9090/VibeCAD/blob/main/docs/ARCHITECTURE.md),
[Agent Architecture](https://github.com/wangtao9090/VibeCAD/blob/main/docs/AGENT_ARCHITECTURE.md),
and the
[Product Capability Roadmap](https://github.com/wangtao9090/VibeCAD/blob/main/docs/PRODUCT_CAPABILITY_ROADMAP.md).
See the
[Integrated Product and Technical Strategy](https://github.com/wangtao9090/VibeCAD/blob/main/docs/PRODUCT_STRATEGY.md)
for the unified decisions on product positioning, open-source strategy, multiple CAD backends,
the AutoCAD/domestic CAD roadmap, and the evaluation framework.

## License

[MIT](LICENSE)
