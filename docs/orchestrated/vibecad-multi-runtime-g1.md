# VibeCAD Multi-Runtime Foundation and G1 Handoff

> Rolling cross-session source of truth for the next VibeCAD campaign.
>
> Artifact revision: `MRG1-R0`
>
> State: `draft / handoff-ready`
>
> Created: `2026-07-26T01:17:24Z`
>
> `MRG1-R0` persists the verified repository state and proposed next-stage
> contract. The user's handoff request authorizes this documentation-only
> write. It does not authorize MR0 or G1 implementation, publication, external
> spend, or installation in another host. A resumed controller must verify this
> artifact and bind one meaningful implementation approval to a later revision
> before changing executable code.

## 1. Context

### 1.1 User goal

VibeCAD is a local-first expert CAD Agent kernel callable by Claude, Codex and
other host Agents. It must let a user create or modify a design through a
reviewable workflow without contaminating the source model. The near-term
product must expose that workflow inside FreeCAD, while the architecture must
not require a Task Kernel, Revision, public-tool or Workbench rewrite when a
second CAD system is added later.

The immediate cross-session goal is:

1. introduce a small generic multi-runtime foundation;
2. make CAD itself a multi-runtime domain while connecting only FreeCAD now;
3. adapt the current FreeCAD worker behind that boundary without changing
   public behavior;
4. deliver the G1 FreeCAD Workbench on the public Task Kernel client;
5. harden P0-B and verify real Claude/Codex hosts before P1 release;
6. add Mechanical3D breadth only after these boundaries are stable.

### 1.2 Verified repository state

- Repository: `/Users/wangtao/Documents/DevProject/vibecad`
- Branch: `codex/agent-stage3`
- Verified `HEAD`: `7d3c5252b076c53863049f7f6433c7e2db220d22`
- Verified upstream: `7d3c5252b076c53863049f7f6433c7e2db220d22`
- `HEAD` and upstream were equal when `MRG1-R0` was created.
- Latest commits:

  ```text
  7d3c525 docs(strategy): define CAD agent evaluation system
  16c45d9 docs(strategy): publish unified VibeCAD product report
  50d7803 docs(orchestration): close P0-B core delivery
  7eb4b3a docs(strategy): consolidate product and backend direction
  157d33f chore(release): package P0-B core as 0.6.0
  ```

- Preexisting untracked user/other-task file:
  `docs/MECHANICAL_DESIGN_VALIDATION_RESEARCH.md`
  - size: `13,194` bytes;
  - lines: `231`;
  - SHA-256:
    `25b849b181cd7315cb148011bc82a7e829a4c80192cf0e00c1574ed0e30751f0`.
- The untracked research file is not part of `MRG1-R0`, must not be staged,
  overwritten or deleted, and is not yet a canonical product commitment.

### 1.3 Completed product baseline

P0-B core is complete at C00-C15. Its authoritative history and evidence remain
in `docs/orchestrated/vibecad-p0b-core.md`.

The completed baseline includes:

- one Task Kernel as Task, Revision, lease, review and commit authority;
- durable project/task/revision/draft/artifact state;
- isolated candidate mutation and source-file preservation;
- Accept, Reject and Revert;
- restart/recovery and HEAD compare-and-swap;
- active cancellation, killable FreeCAD worker generations and reconciliation;
- authenticated daemon, managed checkout and session-bound file grants;
- FCStd and STEP artifact delivery;
- MCP and Workbench backend clients routed through the same kernel;
- version `0.6.0`, runtime epoch `4`, FreeCAD `1.1.0`, MCP `1.27.2`;
- exactly `28` public tools.

Recorded P0-B final gates:

```text
full non-slow:       4902 passed, 108 deselected, 19 warnings
slow Worker/P0-B:      11 passed, 102 deselected
managed public matrix:  2 passed
fresh MCPB:             1 passed
M05 preservation:       1 passed
final review: Critical 0 / Major 0 / Medium 0 / Minor 0
```

These are closeout records, not a claim that `MRG1-R0` reran the suites.
Subsequent commits through `7d3c525` are documentation-only.

### 1.4 Honest current product state

The backend is a local `0.6.0` host-ready candidate. It is not yet a generally
deliverable CAD product because:

- no real Claude/Codex installation has completed canonical host verification;
- no FreeCAD Qt Workbench UI exists;
- G1 preview, verdict, selection and in-FreeCAD Accept/Reject are absent;
- Sketcher, PartDesign, stable face/edge selectors and general imported-model
  editing are absent;
- only six object-level CAD operations are implemented:
  `create_box`, `create_cylinder`, `inspect_model`, `modify_parameter`,
  `move_part`, and `rotate_part`;
- generic FCStd/STEP/STL import, STL-to-CAD reconstruction, photo reconstruction
  and simulation are not delivered;
- no `v0.6.0` tag, GitHub release or marketplace publication exists;
- P0-B retention/GC, runner migration, full operational observability/recovery,
  large-artifact transport and cross-platform evidence remain hardening work.

### 1.5 Known architectural pressure

`CadExecutionPort` is a useful application boundary but is not yet a
multi-CAD contract. Its current types require FCStd and STEP paths, fixed
artifact names, FreeCAD-oriented validation and the current local execution
profile. `WorkerCadExecutionPort` and the worker codec are also FreeCAD
specific. Adding PartDesign breadth directly to these assumptions would make a
future CAD adapter expensive.

### 1.6 Explicit exclusions for the next foundation stage

MR0 must not:

- connect or claim support for a second CAD product;
- implement photo/video reconstruction or simulation;
- create a second Task, Revision, lease, review or commit state machine;
- make Python or arbitrary generated code the primary public execution path;
- implement a universal lowest-common-denominator CAD API;
- silently convert a native model between CAD systems;
- publish a tag, release, PR or marketplace package;
- modify or adopt the untracked research document;
- expand G1 into face/edge editing, semantic diff or TaskPanel parameter tuning.

## 2. Decisions

### MRG1-D01 — One durable authority

The Task Kernel remains the only durable authority for Task state, Revision,
lease, review, Accept/Reject and HEAD commit. Runtime routing never receives
commit authority.

### MRG1-D02 — Two levels of multi-runtime

The architecture has two independent axes:

1. system runtime families such as CAD authoring, reconstruction and
   simulation;
2. multiple runtimes inside the CAD family, such as FreeCAD today and
   SolidWorks, Onshape, Fusion, AutoCAD or another CAD system later.

Only FreeCAD is connected in the current stage.

### MRG1-D03 — Generic lifecycle, domain-specific semantics

The generic runtime layer standardizes only:

- runtime identity, version and capability discovery;
- invocation ownership and Task correlation;
- sealed Revision and immutable Artifact inputs;
- deadlines, resource budgets and execution profile;
- start, status, cancel, health and reconcile;
- immutable output artifacts, provenance, diagnostics and evidence.

CAD commands, reconstruction requests and FEA studies remain different domain
contracts. They must not be forced into one union schema.

### MRG1-D04 — CAD domain boundary

CAD execution is split into:

```text
Task Kernel
  -> CAD Domain Service
  -> CAD capability planning
  -> CAD Runtime registry/router
  -> CAD Runtime adapter
  -> native CAD engine/API
```

The CAD Domain Service owns backend-neutral design intent. A runtime adapter
maps supported intent to one native API. The first adapter is FreeCAD.

### MRG1-D05 — Capability negotiation, not false portability

The common CAD vocabulary covers portable intent such as sketch, constraint,
pad, pocket, hole, fillet, chamfer, pattern, inspect and export. Each runtime
advertises versioned fine-grained capabilities. A planner must choose one of:

- execute natively;
- execute through an explicitly disclosed semantic mapping;
- propose an explicit approximation;
- reject as unsupported before mutation;
- use a namespaced runtime extension for non-portable features.

A capability declaration is evidence, not permission to bypass Task Kernel
validation.

### MRG1-D06 — Native artifacts are runtime-qualified

A Revision must not assume that its native model is always `model.FCStd`.
The intended model is:

```text
Revision
  authoring_runtime
  native_model_artifact
  exchange_artifacts
  semantic_observation
  selector_mapping
  provenance_and_evidence
```

FreeCAD may produce FCStd plus STEP today. A future CAD may use SLDPRT,
document/version references or another native representation. Moving between
authoring runtimes is an explicit conversion/import Task with its own evidence,
not an invisible change to the same Revision.

### MRG1-D07 — Dual selector representation

Persist both:

- a backend-neutral `SemanticSelector` expressing design intent or feature
  lineage; and
- a runtime-specific `NativeLocator` carrying the native persistent reference.

Public tools must not make ephemeral values such as `Face3` the sole durable
identity. Native locator loss triggers re-resolution and evidence, never
guessing.

### MRG1-D08 — FreeCAD adapter first, behavior preserved

The current FreeCAD worker is adapted behind the new CAD runtime boundary.
Existing public tools, Task states, source-safety rules and FCStd/STEP output
must remain behaviorally compatible during MR0. No new CAD breadth is required
to prove the architecture.

### MRG1-D09 — Workbench is a client

The G1 FreeCAD Workbench consumes the authenticated public client, HEAD/draft
resources and file grants. It is not a runtime router or a second authority.
If later GUI-thread CAD execution is needed, it is registered as a FreeCAD GUI
execution profile behind the same adapter and Task Kernel.

### MRG1-D10 — Read-only provider authority

Photo/mesh reconstruction and simulation runtimes consume sealed Revisions or
immutable artifacts and return immutable, provenance-bound artifacts or
proposals. They never commit a CAD Revision. A result that changes a design is
adopted through a new reviewed CAD Task.

### MRG1-D11 — Conformance before a real second runtime

MR0 must include deterministic fake runtime and fake CAD adapter conformance
tests. They must prove that a second runtime identity can register, advertise
capabilities, execute/cancel/reconcile within its declared lifecycle and return
artifacts without Task Kernel changes or commit authority.

### MRG1-D12 — Product milestone order

The intended order is:

1. MR0 generic and CAD multi-runtime foundation;
2. G1 Workbench, P0-B hardening and real-host verification in parallel;
3. P1/G2 Mechanical3D;
4. P1.5 engineering precheck;
5. P2 controlled simulation;
6. P3 enterprise and assembly/delivery;
7. P4 reconstruction providers and additional CAD adapters as demand warrants.

P0-B hardening must close before P1 is called deliverable.

### MRG1-D13 — Evaluation routing

Use the evaluation framework already committed to
`docs/PRODUCT_STRATEGY.md`:

- P0 Trust gates for current reliability;
- G1 preview/selection/review metrics;
- Text2CAD, HistCAD and neuralCAD-Edit subsets for P1/G2;
- DeCoDE CADBench for later photo/STL reconstruction;
- MUSE-style funnel for manufacturing and assembly.

Runtime conformance is an additional architectural gate and does not replace
CAD quality evaluation.

## 3. Proposed Commit Sequence

This sequence is a planning baseline, not implementation authority. A resumed
controller must audit the exact source topology, update this artifact to
`MRG1-R1`, prewrite exact affected tests and obtain one approval for that
revision.

| ID | Proposed commit | Scope | Independent gate |
|---|---|---|---|
| MR0-C00 | `docs(architecture): define multi-runtime CAD contracts` | Canonical architecture/roadmap terms, authority diagram, runtime vs adapter vs provider | docs links, terminology search, review finds no second authority or delivered-runtime overclaim |
| MR0-C01 | `feat(runtime): add runtime capability contracts` | Runtime descriptor, identity, invocation/result envelope and lifecycle control ports; deterministic fake | focused RED/GREEN for identity, capability, budgets, cancel/reconcile, provenance and immutable results |
| MR0-C02 | `feat(cad): add backend-neutral CAD runtime port` | CAD runtime identity, native-model artifact, semantic/native selector envelope and namespaced capability model | focused RED/GREEN registers two fake CAD identities without Task Kernel or public-tool changes |
| MR0-C03 | `refactor(freecad): adapt worker to CAD runtime port` | FreeCAD adapter wraps current worker behavior; remove core assumptions only where required | existing FreeCAD worker/Task/revision/public-tool suites plus real managed FreeCAD smoke |
| MR0-C04 | `test(runtime): enforce adapter conformance` | Generic runtime and CAD adapter conformance kit, authority-negative cases and compatibility matrix | fake runtime/adapters pass; commit-bypass, unsupported capability and artifact mismatch cases fail closed |
| MR0-C05 | `docs(orchestration): close multi-runtime foundation` | Evidence, residuals, diagrams, developer adapter guide and recovery snapshot | full affected suite, full non-slow, selected real FreeCAD gates, independent architecture and diff reviews |

Follow-on campaigns are intentionally separate:

- `G1`: FreeCAD Qt Workbench Dock, HEAD/draft preview, verdict,
  stale/revoked rejection, Accept/Reject and object/feature capture.
- `P0BH`: retention/GC, runner upgrade/migration, operational recovery
  observability and large-artifact transport.
- `HOST1`: canonical real Claude and Codex host activation and task evidence.
- `P1/G2`: Sketcher, PartDesign, stable selectors, controlled import,
  STL-to-faceted-STEP and TaskPanel tuning.

## 4. Manual Validation Matrix

| ID | Environment and scenario | Expected observation | Owner / user presence |
|---|---|---|---|
| MRG1-M01 | Installed managed FreeCAD 1.1.0; run existing empty/direct/program candidate flow through the FreeCAD adapter | Same Task lifecycle and FCStd/STEP evidence as the P0-B baseline; source bytes unchanged | controller; user not required |
| MRG1-M02 | Register FreeCAD and a deterministic fake CAD runtime | Capability discovery selects only declared operations; unsupported intent fails before candidate mutation | controller; user not required |
| MRG1-M03 | Cancel and lose a fake runtime invocation, then restart/reconcile | One durable Task lineage converges without a second job authority or duplicate execution | controller; user not required |
| MRG1-M04 | Review Revision manifest emitted by FreeCAD adapter | Runtime-qualified native FCStd and STEP exchange artifacts have hashes, provenance and selector evidence | independent reviewer; user not required |
| G1-M01 | FreeCAD GUI with future Workbench | User sees HEAD and draft, verdict and stale/revoked status, then Accept or Reject without source pollution | user useful for product acceptance, not required for mechanical gate |
| HOST1-M01 | Real Claude/Codex host | Host discovers capabilities, completes one canonical task and receives FCStd/STEP resources | user required only for subscription-host activation if the host demands it |

## 5. Budget and Circuit Breakers

### 5.1 Proposed limits

- MR0 implementation budget: at most six semantic commits including docs
  closeout.
- One semantic concern per commit; no opportunistic P1 or G1 implementation.
- No more than one generic runtime lifecycle abstraction and one CAD runtime
  domain abstraction.
- Preserve the existing public protocol and 28-tool projection unless a later
  approved revision explicitly changes them.

### 5.2 Circuit breakers

Stop implementation and append evidence if any of these becomes observable:

- Task Kernel, Revision or Accept/Reject logic must be duplicated per runtime;
- the common runtime layer imports FreeCAD, FCStd, Qt, FEA or reconstruction
  domain types;
- a runtime or provider can advance HEAD directly;
- adapting FreeCAD requires changing public tool semantics or losing current
  artifact/source-safety guarantees;
- a proposed common CAD operation is only a disguised FreeCAD object/property
  API;
- selectors rely solely on unstable runtime-native face/edge indices;
- a second CAD is claimed supported without real adapter and native-engine
  evidence;
- an unexpected tracked or untracked path appears;
- the preexisting research file changes hash without explicit user ownership;
- focused tests, real FreeCAD smoke, full non-slow suite, diff review or
  repository integrity is red;
- scope requires external spend, credentials, publication, force-push or an
  irreversible external action.

At a breaker, preserve the workspace and evidence. Do not reset, rewrite,
force, hide or automatically expand the allowlist.

## 6. Proposed File Allowlist

The exact implementation allowlist must be narrowed in `MRG1-R1` after source
audit. Its maximum proposed envelope is:

```text
docs/ARCHITECTURE.md
docs/AGENT_ARCHITECTURE.md
docs/PRODUCT_CAPABILITY_ROADMAP.md
docs/PRODUCT_STRATEGY.md
docs/ACCEPTANCE_TESTS.md
docs/orchestrated/vibecad-multi-runtime-g1.md
src/vibecad/runtime/**
src/vibecad/interaction/cad.py
src/vibecad/execution/**
src/vibecad/worker/**
src/vibecad/workflow/**
src/vibecad/application/**
tests/**
```

Restrictions:

- broad directory entries are discovery envelopes, not permission for unrelated
  cleanup;
- `MRG1-R1` must list the exact paths per commit before implementation;
- packaging/version/public tools remain out of scope unless an approved
  revision adds exact files and compatibility gates;
- `docs/MECHANICAL_DESIGN_VALIDATION_RESEARCH.md` is explicitly denied.

## 7. Expected Impact

### 7.1 Intended impact

- no user-visible behavior change during MR0;
- no change to the sole Task Kernel or source-safety workflow;
- FreeCAD becomes the first registered CAD adapter instead of a core
  assumption;
- future CAD support is primarily an adapter, capability mapping, selector
  mapping and conformance effort;
- G1 remains runtime-neutral at its public-client boundary;
- future reconstruction and simulation can reuse lifecycle/provenance
  infrastructure without receiving CAD authority.

### 7.2 Expected test impact

- new focused contract and conformance tests;
- updates to tests that construct `CadExecutionPort` directly;
- existing Task Kernel, workflow, worker, MCP and public-tool tests should
  remain green;
- managed FreeCAD tests must continue producing exact native/exchange evidence;
- no expected reduction in test count or weakening of negative assertions;
- full baseline counts may increase, but any disappearance or skip increase is
  a breaker until explained.

### 7.3 First user-visible outcome after MR0

G1 must let a user:

1. connect the FreeCAD Workbench to a VibeCAD project;
2. see current HEAD and the isolated Agent draft;
3. inspect the verdict and stale/revoked state;
4. Accept or Reject inside FreeCAD;
5. receive a new Revision and FCStd/STEP on Accept;
6. observe no source-file mutation on Reject or failure.

## 8. Residuals

| ID | Evidence | Impact | Owner | Disposition / closure condition |
|---|---|---|---|---|
| MRG1-RES-01 | `CadExecutionPort` currently requires FCStd/STEP-specific validation and names | Multi-CAD adapter would leak FreeCAD assumptions | MR0 | close when native artifacts and CAD adapter contract are runtime-qualified and existing behavior passes |
| MRG1-RES-02 | No generic runtime registry/invocation/result conformance exists | Reconstruction/simulation or another CAD could create parallel lifecycle logic | MR0 | close with C01/C04 conformance and authority-negative tests |
| MRG1-RES-03 | No FreeCAD Qt Workbench UI | Ordinary user cannot review inside CAD | G1 | close with G1 visual/manual acceptance |
| MRG1-RES-04 | Real Claude/Codex host not verified | host-ready is not host-verified | HOST1 | close with canonical task/resource evidence from each claimed host |
| MRG1-RES-05 | P0-B hardening residuals remain | blocks P1 deliverable claim | P0BH | inherit authoritative IDs from P0-B; close before P1 release |
| MRG1-RES-06 | Research document is untracked and not canonical | product validation/simulation direction may diverge from committed strategy | user/product review | preserve exact file; close only by explicit adopt/revise/discard decision |
| MRG1-RES-07 | No second CAD adapter exists | architecture is conformance-ready, not multi-CAD product support | later demand-led stage | close only with a real adapter, engine evidence and product acceptance |
| MRG1-RES-08 | No photo/STL reconstruction or simulation runtime | advanced workflow remains reserved only | P1.5/P2/P4 | close per a separately approved provider/runtime stage |

## 9. Authorization History

| ID | Time | Artifact revision | Scope | Exact user authorization | State |
|---|---|---|---|---|---|
| MRG1-A00 | 2026-07-26T01:17:24Z | MRG1-R0 | Documentation-only state persistence and handoff | `状态持久化一下  我新开一个会话执行接下来的计划` | completed when this artifact is written and verified; no implementation authority |

Earlier product-direction wording preserved as input to this artifact:

```text
包括 把多 runtime 的架构提前埋好
cad 也需要做好多 runtime 的工作 当然目前仅接入 freecad
主要不想以后接其他 cad 工具的时候 需要大返工
```

## 10. Capability Profile and Adapter Selection

Selected adapter: Codex desktop.

```text
approval: native-plan
delegation: spawn-send-wait
persistence: repo-artifact
process: native-session-poll
```

Execution restriction for `MRG1-A00`: perform the handoff serially. No
implementation delegation is needed, and no subagent claim is made.

Permitted capability evidence categories:

- `live capability declarations`: `update_plan`, collaboration
  spawn/message/wait operations, `exec_command`, `write_stdin` and
  `apply_patch` are declared in the current session.
- `observable behavior`: `update_plan`, synchronous read-only commands and
  `apply_patch` calls return observable results in the current session; no
  long-running session was needed for this documentation packet.
- `environment identity`: Codex desktop exposes repository
  `/Users/wangtao/Documents/DevProject/vibecad`, macOS workspace context,
  branch context and current date/time passively.
- `public configuration`: Git exposes branch/upstream configuration read-only;
  no other public configuration evidence was observed.

No credentials, tokens, keys, secrets, repository contents or prior approval
artifacts are classified as capability evidence.

## 11. Recovery Snapshot MRG1-S00

### 1. Completed milestones

- P0-B C00-C15 is closed and pushed; product/package end is
  `157d33f89386499dfbf3d589cd8a57ffffcde434`; orchestration closeout is
  `50d78033b45e7eaeace991702346244a98d558f0`.
- Product/backend strategy consolidation is
  `7eb4b3a92a937e005509d75b8d6b111b134a9350`; the unified product report is
  `16c45d9204ac5f5eb044a5f79c04971103d56fad`; CAD Agent evaluation is
  `7d3c5252b076c53863049f7f6433c7e2db220d22`.
- At snapshot creation, `HEAD` and upstream are both `7d3c525...`.
- P0-B recorded gates remain `4902` non-slow, `11` slow, `2` managed,
  `1` MCPB and `1` preservation test; final review is `0/0/0/0`.
- Artifact revision `MRG1-R0` persists the current product state,
  MRG1-D01..D13, proposed MR0-C00..C05 and the next campaign boundary.
- The preexisting untracked research file is preserved at
  `25b849b181cd7315cb148011bc82a7e829a4c80192cf0e00c1574ed0e30751f0`.

### 2. Next steps

1. In the new session, read this artifact completely and inspect
   `git status --short --branch`, `git rev-parse HEAD`,
   `git rev-parse @{upstream}` and the research-file hash.
2. If `HEAD=upstream=7d3c525...` and porcelain contains only the research file
   plus this handoff artifact, continue with a read-only MR0 source/test audit.
   Do not treat the handoff file itself as an implementation approval.
3. If the branch advanced only by a commit containing this exact handoff
   artifact, record the new commit as a documentation anchor and continue the
   read-only audit. If it advanced for another reason, inspect and append a
   recovery correction before planning.
4. If the research hash differs, preserve it as user-owned state, do not edit
   it, and append the observed hash/status before proceeding.
5. Produce `MRG1-R1` with exact per-commit file allowlists, focused RED/GREEN
   commands, real FreeCAD gates, independent reviews and implementation
   circuit breakers.
6. Present one meaningful approval: “introduce the multi-runtime and
   multi-CAD foundation while preserving all current FreeCAD behavior and
   public tools.” Do not request approval for internal interruptions already
   covered by the approved packet.
7. After approval, execute MR0-C00..C05. Then create separate bounded packets
   for G1, P0-B hardening and real-host verification.

Observable branches:

- unexpected tracked changes: stop before implementation and preserve them;
- only known user-owned untracked research: continue read-only planning around
  it;
- MR0 requires public behavior change: breaker, revise artifact and reapprove;
- FreeCAD compatibility gate red: preserve evidence and repair only inside the
  approved MR0 allowlist;
- need for external credentials, spend or publication: stop for user authority.

### 3. Approved decisions

- `MRG1-A00` authorizes only creation and verification of this handoff
  artifact.
- Product direction carried into the next plan is MRG1-D01..D13:
  one Task Kernel; generic runtime lifecycle; CAD-domain multi-runtime;
  FreeCAD-only current adapter; runtime-qualified native artifacts; dual
  selectors; capability negotiation; Workbench as client; providers without
  commit authority; conformance before a real second runtime.
- No MR0/G1/P0BH/HOST1 code implementation, PR, tag, release, publication,
  external installation or spend is authorized by `MRG1-A00`.
- The untracked mechanical validation research remains independent and is not
  adopted by this artifact.

### 4. Execution discipline

- Capability profile:
  `native-plan / spawn-send-wait / repo-artifact / native-session-poll`.
- Adapter: Codex desktop; authoritative persistence:
  `docs/orchestrated/vibecad-multi-runtime-g1.md`.
- Current documentation-only allowlist is exactly this artifact. The existing
  research file is denied.
- Handoff gates: exact artifact existence/content, Markdown whitespace check,
  `git diff --check`, porcelain inspection, verified HEAD/upstream equality and
  research hash preservation.
- Implementation gates and allowlists remain proposals until `MRG1-R1`.
- Circuit breakers and residual rules are Sections 5 and 8.
- Recovery commands:

  ```text
  git status --short --branch
  git rev-parse HEAD
  git rev-parse @{upstream}
  git log -5 --oneline
  shasum -a 256 docs/MECHANICAL_DESIGN_VALIDATION_RESEARCH.md
  git diff --check
  ```

- Never reset, rewrite, force, hide or absorb user-owned changes. Never infer
  implementation continuity from chat memory; recover from Git and this
  artifact.
