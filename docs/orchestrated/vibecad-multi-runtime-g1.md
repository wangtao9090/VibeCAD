# VibeCAD Multi-Runtime Foundation and G1 Handoff

> Rolling cross-session source of truth for the next VibeCAD campaign.
>
> Artifact revision: `MRG1-R1`
>
> State: `approved / executing`
>
> Created: `2026-07-26T01:17:24Z`
>
> Revised: `2026-07-26T01:30:51Z`
>
> `MRG1-R0` persists the verified repository state and proposed next-stage
> contract. The user's handoff request authorizes this documentation-only
> write. It does not authorize MR0 or G1 implementation, publication, external
> spend, or installation in another host. A resumed controller must verify this
> artifact and bind one meaningful implementation approval to a later revision
> before changing executable code.
>
> `MRG1-R1` appends the completed MR0 source audit, exact implementation
> allowlist, prewritten gates, revised capability route and one meaningful
> approval boundary. At R1 creation it remained unapproved; MRG1-A01 in
> Section 17 supersedes that historical pre-approval state.

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

## 12. MRG1-R1 MR0 Source Audit

### 12.1 Recovery result

MRG1-S00 was recovered without relying on chat memory:

- branch: `codex/agent-stage3`;
- recovered `HEAD` and upstream:
  `04045da62822af964e04140b43620469d2841c61`;
- the only commit after the old `7d3c525...` anchor is
  `04045da docs(orchestration): hand off multi-runtime foundation`;
- `git show --name-status 04045da...` proves that commit added only this
  artifact;
- the only remaining untracked path is
  `docs/MECHANICAL_DESIGN_VALIDATION_RESEARCH.md`;
- that file remains `13,194` bytes, `231` lines and SHA-256
  `25b849b181cd7315cb148011bc82a7e829a4c80192cf0e00c1574ed0e30751f0`;
- the user's new wording,
  `未跟踪的机械验证研究文档找机会一起提交`, authorizes an exact,
  non-opportunistic documentation inclusion in MR0-C00. It does not turn the
  report's P1/P2 directions into committed delivery scope.

No unexpected tracked or untracked path was observed.

### 12.2 Current source topology

The audit found four distinct boundaries that must not be conflated:

1. `src/vibecad/runtime/` currently means managed FreeCAD installation,
   receipt, status and platform selection. It has no generic provider
   identity, invocation/result envelope, lifecycle port or registry.
2. `src/vibecad/interaction/cad.py` defines the nominal
   `CadExecutionPort`, but the contract combines CAD-domain execution with a
   FreeCAD-shaped lifecycle:
   - `ExecutionProfile` is described as closed FreeCAD surfaces;
   - import and materialization evidence are FCStd/STEP-specific;
   - `CandidateEvidence` requires exactly `model.FCStd` then `model.step`;
   - `validate_materialization()` takes named `fcstd` and `step` paths;
   - `export_step()` is part of the core execution port.
3. `src/vibecad/execution/worker_port.py` is already the concrete
   FreeCAD/process boundary. It owns worker generation fencing, opaque
   candidate/revision capabilities, cancellation, validation, checkpoint,
   STEP export, reload and evidence collection. It is the correct component to
   adapt; the Task Kernel must not learn worker RPC details.
4. `src/vibecad/execution/revisions.py` is a large crash-consistent durable
   store with fixed FCStd/STEP layout throughout import, candidate creation,
   sealing, manifest validation, copy, recovery and reconciliation. The audit
   counted `67` direct `model.FCStd`/`model.step` occurrences in this file and
   `18` source modules containing one or both names.

The authority chain remains sound:

```text
AgentApplication
  -> project-scoped TaskService
  -> CandidateCoordinator + CadExecutionPort
  -> LocalRevisionStore
  -> HEAD compare-and-swap
```

`TaskService` auto-commit and `accept_draft()` both converge on
`CandidateCoordinator.commit()`, the sole caller of
`LocalRevisionStore.commit_revision()` and HEAD advancement.
`WorkerCadExecutionPort` is the trusted application-owned parent; it retains
the existing store/lease capabilities only for bounded candidate/revision
validation, checkpoint/export/reload and evidence, and its adapter methods do
not call commit, Accept/Reject or HEAD mutation. The child Worker codec/service
receive no store/lease objects or daemon credentials, and their closed
protocol exposes no commit, review or HEAD method.

### 12.3 Selector and operation findings

- `SelectorV1` is already revision-bound and backend-neutral at object/feature
  level: it carries project/revision, VibeCAD UUIDs, type, semantic role,
  provenance and exact cardinality.
- It intentionally has no face/edge locator and never falls back to FreeCAD
  labels or ephemeral sub-element indices.
- MR0 therefore must wrap the existing semantic selector in a dual
  semantic/native adapter envelope without changing the public SelectorV1
  wire contract. A native locator may strengthen execution evidence but may
  not replace semantic identity.
- The operation registry already separates allowlisted semantic commands from
  handlers, but `ExecutionProfile` still combines runtime product and
  execution mode. MR0 adds runtime identity/capability planning around the
  registry; it does not add a seventh public CAD operation.

### 12.4 Composition findings

- `_default_cad_port_factory()` in
  `src/vibecad/application/project.py` is the narrow default composition seam.
- `AgentApplication` lazily constructs one port through that factory and
  serializes it behind the existing CAD gate.
- `build_project_runtime()` and `TaskService` accept the nominal
  `CadExecutionPort`; preserving that nominal boundary allows a CAD Domain
  Service/router to be inserted without changing Task, Revision, review,
  Accept/Reject or public-tool behavior.
- Project bootstrap and artifact validation also consume the same nominal
  port. The FreeCAD adapter must continue to expose the exact validation
  behavior until durable artifact migration is separately approved.

### 12.5 Audit baseline evidence

Commands were run from
`/Users/wangtao/Documents/DevProject/vibecad` at
`04045da62822af964e04140b43620469d2841c61`.

| Evidence ID | Command / result |
|---|---|
| MRG1-GA01 | `pytest ...` from the bare shell: exit `127`, global `pytest` absent; did not enter tests |
| MRG1-GA02 | locked local environment, five affected files: `375 passed, 23 deselected in 19.29s` |
| MRG1-GA03 | full non-slow: `4902 passed, 108 deselected, 19 warnings in 156.42s` |
| MRG1-GA04 | `.venv/bin/python -m ruff check src tests`: exit `0`, `All checks passed!` |
| MRG1-GA05 | managed FreeCAD worker load/modify/checkpoint/export: `1 passed in 8.74s` |
| MRG1-GA06 | Task Kernel slow gate with `VIBECAD_FREECAD_ENV` override: setup error, correctly rejected before product path |
| MRG1-GA07 | same Task Kernel gate against the current managed generation, no override: `1 passed in 4.82s` |

The two setup failures are preserved as environment evidence, not counted as
product regressions or RED tests.

### 12.6 Audit conclusion

MR0 can safely establish and connect the multi-runtime boundary without
rewriting the durable revision store. It cannot honestly close the whole
fixed-artifact migration in six commits. Doing so would couple a new
architecture boundary to high-risk persistence/recovery changes and violate
the stage budget.

MRG1-D06 is therefore split:

- MR0 makes runtime identity, capability planning, invocation lifecycle,
  runtime-qualified artifact profiles and dual selector envelopes generic,
  then maps the existing FreeCAD FCStd/STEP layout through the adapter.
- `MR1` will migrate `RevisionRef`, candidate/revision storage, artifact
  materialization and recovery from fixed filenames to a versioned durable
  artifact profile. Until MR1 closes, a second native CAD format cannot be
  persisted and no second CAD support may be claimed.

## 13. MRG1-R1 Approved-When-Authorized Stage Contract

### 13.1 Context and success criteria

MR0 succeeds only if:

1. generic runtime contracts express immutable identity, capabilities,
   invocation ownership, Task correlation, sealed inputs, budget/deadline,
   lifecycle, immutable result artifacts, provenance, diagnostics and
   evidence without importing CAD, FreeCAD, Qt, FEA or reconstruction types;
2. the CAD domain adds backend-neutral runtime identity, capability decisions,
   runtime-qualified artifact profiles and semantic/native selector envelopes;
3. two deterministic fake CAD identities can register and plan independently
   without any Task Kernel or public-tool change;
4. the current worker is selected as the FreeCAD adapter through a CAD Domain
   Service/router at the default composition seam;
5. every existing FreeCAD lifecycle, source-safety, FCStd/STEP result,
   cancellation and recovery behavior remains green;
6. the public surface remains exactly `28` tools and no second CAD is claimed;
7. the mechanical validation research is committed as direction research,
   explicitly not as a promised MR0/P1/P2 feature.

Explicit exclusions remain G1 UI, P0-B hardening, HOST1 activation, new CAD
operations, face/edge editing, durable artifact schema migration, a real
second CAD, reconstruction, simulation, publication, release, external spend
and credentials.

### 13.2 Additional decisions

#### MRG1-D14 — Adapter-first durable migration split

MR0 routes the current fixed durable layout through a runtime-qualified
FreeCAD adapter. `RevisionRef` and `LocalRevisionStore` remain unchanged in
MR0. MR1 is the only stage allowed to generalize their durable schema.

Rationale: preserve proven crash consistency and isolate architecture risk
from persistence migration risk.

#### MRG1-D15 — Public compatibility projection

The public `SelectorV1`, six operations, `CadExecutionPort` behavior and
28-tool surface remain compatibility projections during MR0. New runtime
identity, native locators and artifact profiles are internal adapter/domain
contracts until a separately versioned public protocol is approved.

#### MRG1-D16 — Mechanical research status

`docs/MECHANICAL_DESIGN_VALIDATION_RESEARCH.md` is accepted into source control
as a product-direction research artifact. Its existing statement that it does
not constitute committed scope remains authoritative. MR0 may cross-link it
but may not implement its P1/P1.5/P2 roadmap.

### 13.3 Exact commit sequence

| ID | Commit | Exact scope | Independent gate |
|---|---|---|---|
| MR0-C00 | `docs(architecture): define multi-runtime CAD boundary` | update canonical architecture/strategy/roadmap/acceptance terms; add the already-written mechanical validation research; no executable code | named-file allowlist, relative-link check, terminology/overclaim search, `git diff --check`, distinct docs review |
| MR0-C01 | `feat(runtime): add runtime capability contracts` | immutable generic runtime descriptor, capability, invocation/result, artifact/provenance, lifecycle status/control port and deterministic registry | genuine import/contract RED; focused immutable/strict/duplicate/budget/cancel/reconcile GREEN; common layer import-purity check |
| MR0-C02 | `feat(cad): add backend-neutral CAD runtime port` | CAD runtime descriptor, capability decision, artifact profile, semantic/native selector envelope, adapter registry/router and domain service | genuine RED; two fake CAD identities plan without Task Kernel edits; unsupported/mapping/approximation/extension branches are exact and mutation-free |
| MR0-C03 | `refactor(freecad): route worker through CAD runtime adapter` | make current worker port the FreeCAD adapter; compose it through the CAD Domain Service at the existing default factory; preserve old nominal port | focused composition RED/GREEN; affected 375-test baseline; public 28-tool checks; managed worker and Task Kernel real gates |
| MR0-C04 | `test(runtime): enforce adapter conformance` | reusable generic runtime and CAD adapter conformance kits; authority-negative, artifact mismatch and unsupported capability cases | deterministic fake runtime/adapters pass; commit/HEAD-like authority, runtime/artifact mismatch, sole ephemeral selector and undeclared capability fail closed |
| MR0-C05 | `docs(orchestration): close multi-runtime foundation` | developer adapter guide, acceptance evidence, residuals, ledger and recovery snapshot | full non-slow, ruff, exact slow gates, public-surface count, diff/allowlist, architecture and adversarial review |

No implementation commit may start until its predecessor is pushed and its
ledger row is appended. The stage budget remains exactly six commits.

### 13.4 Exact per-commit file allowlist

MR0-C00:

```text
docs/ARCHITECTURE.md
docs/AGENT_ARCHITECTURE.md
docs/PRODUCT_CAPABILITY_ROADMAP.md
docs/PRODUCT_STRATEGY.md
docs/ACCEPTANCE_TESTS.md
docs/MECHANICAL_DESIGN_VALIDATION_RESEARCH.md
docs/orchestrated/vibecad-multi-runtime-g1.md
```

MR0-C01:

```text
src/vibecad/runtime/__init__.py
src/vibecad/runtime/contracts.py
src/vibecad/runtime/registry.py
tests/test_runtime_contracts.py
tests/test_runtime_registry.py
docs/orchestrated/vibecad-multi-runtime-g1.md
```

MR0-C02:

```text
src/vibecad/interaction/__init__.py
src/vibecad/interaction/cad_runtime.py
tests/test_cad_runtime.py
docs/orchestrated/vibecad-multi-runtime-g1.md
```

MR0-C03:

```text
src/vibecad/application/project.py
src/vibecad/execution/worker_port.py
tests/test_agent_application.py
tests/test_cad_execution_port.py
tests/test_freecad_worker.py
docs/orchestrated/vibecad-multi-runtime-g1.md
```

MR0-C04:

```text
src/vibecad/runtime/conformance.py
src/vibecad/interaction/cad_conformance.py
tests/test_runtime_conformance.py
tests/test_cad_runtime_conformance.py
tests/test_runtime_purity.py
docs/orchestrated/vibecad-multi-runtime-g1.md
```

MR0-C05:

```text
docs/ACCEPTANCE_TESTS.md
docs/ARCHITECTURE.md
docs/CAD_RUNTIME_ADAPTER_GUIDE.md
docs/orchestrated/vibecad-multi-runtime-g1.md
```

The artifact appears in every commit only for append-only gate/ledger
evidence. No broad `src/**`, `tests/**` or `docs/**` allowance remains.

### 13.5 Prewritten RED/GREEN and gate commands

All Python gates use the existing locked `.venv`; a bare global `pytest`
command is prohibited.

MR0-C01 RED/GREEN:

```text
PYTHONPATH=src .venv/bin/python -m pytest -q \
  tests/test_runtime_contracts.py tests/test_runtime_registry.py
```

MR0-C02 RED/GREEN:

```text
PYTHONPATH=src .venv/bin/python -m pytest -q \
  tests/test_runtime_contracts.py tests/test_runtime_registry.py \
  tests/test_cad_runtime.py
```

MR0-C03 focused compatibility:

```text
PYTHONPATH=src .venv/bin/python -m pytest -q \
  tests/test_cad_execution_port.py tests/test_execution_adapter.py \
  tests/test_freecad_worker.py tests/test_task_service.py \
  tests/test_task_kernel_integration.py -m "not slow"

PYTHONPATH=src .venv/bin/python -m pytest -q \
  tests/test_agent_application.py tests/test_project_bootstrap.py \
  tests/test_server_agent_surface.py tests/test_p0b_acceptance.py \
  -m "not slow"
```

MR0-C03 real FreeCAD:

```text
VIBECAD_MANAGED_FREECAD_PYTHON="<current managed python>" \
PYTHONPATH=src .venv/bin/python -m pytest -q -m slow \
  tests/test_freecad_worker.py::test_real_managed_worker_load_modify_checkpoint_and_export

VIBECAD_RUN_INTEGRATION=1 PYTHONPATH=src \
.venv/bin/python -m pytest -q -m slow \
  tests/test_task_kernel_integration.py::test_real_task_kernel_commits_verified_candidate \
  tests/test_task_kernel_integration.py::test_real_agent_first_public_matrix_and_cross_process_review
```

No `VIBECAD_FREECAD_ENV` override is permitted for the Agent-first Task Kernel
gate; it must use the current managed generation.

MR0-C04 conformance:

```text
PYTHONPATH=src .venv/bin/python -m pytest -q \
  tests/test_runtime_contracts.py tests/test_runtime_registry.py \
  tests/test_cad_runtime.py tests/test_runtime_conformance.py \
  tests/test_cad_runtime_conformance.py tests/test_runtime_purity.py
```

MR0-C05 final:

```text
PYTHONPATH=src .venv/bin/python -m pytest -q
.venv/bin/python -m ruff check src tests
git diff --check
```

Each behavior commit must first produce a genuine focused RED caused by the
missing intended contract or route. Import errors are acceptable only for a
new, deliberately absent module; syntax/setup/dependency failures are not RED
evidence. Every GREEN records exit status, count and relevant descriptor/hash
facts in the ledger.

### 13.6 Manual validation matrix

| ID | Environment / scenario | Expected observation | Owner |
|---|---|---|---|
| MRG1-M01 | current managed FreeCAD 1.1.0 through FreeCAD adapter | exact candidate lifecycle, reload, FCStd/STEP hashes and source preservation remain | controller |
| MRG1-M02 | FreeCAD plus deterministic fake CAD descriptors | only declared capability is planned; fake identity does not become delivered support | controller review |
| MRG1-M03 | cancel, lose and reconcile deterministic runtime invocation | one Task correlation and one runtime invocation converge without commit authority or duplicate execution | controller review |
| MRG1-M04 | FreeCAD adapter artifact/selector evidence | native FCStd and exchange STEP are runtime-qualified; semantic selector remains authoritative and no bare Face/Edge index is durable | distinct review pass |
| MRG1-M05 | docs and mechanical research | research is discoverable and cross-linked but still says “not committed scope”; no simulation delivery claim | product review |

The user is not required for mechanical validation during MR0. Product
acceptance remains G1-M01.

### 13.7 Budgets and circuit breakers

The existing Section 5 breakers remain active, plus:

- any required edit to `src/vibecad/execution/revisions.py`, revision manifest
  schema, candidate store layout or recovery journal stops MR0 and opens MR1;
- any public protocol/schema/tool-count change stops before implementation;
- any adapter method can write HEAD, accept/reject a Task or obtain daemon
  credentials;
- the generic runtime layer imports `vibecad.interaction`, CAD, FreeCAD, Qt,
  FEA or reconstruction modules;
- the CAD common layer imports FreeCAD, FCStd-specific implementation types or
  worker codec/proxy/service modules;
- the FreeCAD adapter changes an existing error code, artifact name/format,
  task transition, cancellation outcome or source-preservation invariant;
- any fake runtime is described as product support;
- the mechanical research file changes product status or scope wording beyond
  link/format corrections;
- an unexpected path appears, a gate is unexpectedly red, the full test count
  decreases, deselections/warnings increase without exact explanation, or the
  six-commit budget is exceeded.

At a breaker, preserve the workspace, append evidence and stop. Do not expand
the allowlist or weaken assertions in the same packet.

### 13.8 Expected impact

Expected implementation impact:

- new internal runtime and CAD-domain modules plus conformance fixtures;
- one changed default composition seam;
- new FreeCAD adapter metadata and delegation;
- zero public tool, Task state, Revision schema, artifact payload or
  user-visible behavior change;
- test count increases; existing `4902` non-slow tests remain present;
- no version, runtime epoch, manifest or package change.

Expected first user-visible product outcome remains G1: in-FreeCAD HEAD/draft
preview, verdict, stale/revoked state and Accept/Reject. MR0 is valuable
because that client stays attached to one Task Kernel while future runtimes
remain replaceable behind the CAD Domain Service.

### 13.9 Residual revisions

| ID | Evidence | Impact | Owner / closure |
|---|---|---|---|
| MRG1-RES-01A | durable revision/candidate/artifact store still fixes FCStd/STEP in 18 modules after MR0 | second native CAD cannot persist yet | MR1; versioned schema migration with crash/recovery matrix |
| MRG1-RES-06A | mechanical validation report is now approved for source control as research | direction is discoverable but hypotheses remain unvalidated | product research; user studies or separately approved stage |
| MRG1-RES-09 | public `ExecutionProfile` remains a compatibility projection | public protocol does not expose runtime identity yet | later versioned public protocol; no change in MR0 |

MRG1-RES-01 is superseded by the narrower MR0 adapter closure and
MRG1-RES-01A durable migration. MRG1-RES-06 closes when MR0-C00 commits the
unchanged research status; MRG1-RES-06A remains.

## 14. MRG1-R1 Implementation Approval Gate

### 14.1 Product-meaningful approval to present

The implementation authorization must be explicit and bind this exact
revision. The proposed wording is:

```text
我批准 MRG1-R1：在保持当前 28 个公共工具、Task/Revision/Accept/Reject
唯一权威、源文件安全以及 FreeCAD 的 FCStd/STEP 行为完全兼容的前提下，
执行 MR0-C00..C05，引入通用多 runtime 生命周期、CAD 多 runtime
能力规划与路由、运行时限定产物和双选择器契约，并把当前 FreeCAD Worker
接到该 adapter 边界；同时将机械详细设计与仿真验证调研作为“方向研究、
非已承诺功能”纳入源码。此次批准不包含 durable RevisionStore schema
迁移、第二 CAD 支持、G1 UI、新建模能力、仿真、发布、外部安装、费用或凭据。
```

This approval produces a product-relevant architecture outcome: future CAD
runtimes can be planned and adapted without duplicating Task Kernel authority,
while current users keep exactly the trusted FreeCAD behavior they have.

### 14.2 Authorization state

`MRG1-A01` is reserved for the user's exact approval of Section 14.1.

State: `awaiting user approval`.

No executable source, test, research file or canonical product document may be
edited under MRG1-A01 until that approval is received. After approval, all
MR0-C00..C05 packets inherit it and must not ask again unless scope, authority,
irreversibility or an observable circuit breaker changes.

## 15. MRG1-R1 Capability Profile and Adapter Selection

Selected adapter: Codex desktop.

```text
approval: native-plan
delegation: serial
persistence: repo-artifact
process: native-session-poll
```

Permitted capability evidence categories:

- `live capability declarations`: `update_plan`, `exec_command`,
  `write_stdin` and `apply_patch` are declared in the current session.
  Collaboration tools are also declared, but the current higher-priority
  session rule prohibits proactive subagent spawning unless the user or an
  applicable instruction explicitly asks for it; this campaign therefore
  selects `serial`.
- `observable behavior`: `update_plan` updated the native projection;
  `exec_command` returned controllable sessions `63194`, `65389`, `29405`,
  `30011` and `99321`; `write_stdin` polled each original session through
  real completion without relaunch.
- `environment identity`: Codex desktop passively exposes the macOS workspace,
  repository root, current date/time and America/Los_Angeles timezone.
- `public configuration`: read-only Git configuration exposes branch
  `codex/agent-stage3` and upstream `origin/codex/agent-stage3`; no other
  relevant public configuration was observed.

No repository content, artifact approval, credential, token, key, secret or
private memory is classified as capability evidence.

The native plan is only a projection. This artifact remains authoritative.
Serial execution still requires a distinct review pass and all per-commit
gates.

## 16. Recovery Snapshot MRG1-S01

### 1. Completed milestones

- Recovered S00 at
  `HEAD=upstream=04045da62822af964e04140b43620469d2841c61`.
- Verified the only post-S00-anchor commit contains this handoff artifact.
- Completed MR0 source/test audit and appended artifact revision `MRG1-R1`.
- Reproduced full non-slow baseline:
  `4902 passed, 108 deselected, 19 warnings`.
- Reproduced affected baseline: `375 passed, 23 deselected`.
- Ruff is green; managed FreeCAD worker and Task Kernel selected gates are
  each `1 passed`.
- Research file remained unchanged at
  `25b849b181cd7315cb148011bc82a7e829a4c80192cf0e00c1574ed0e30751f0`.
- No MR0 implementation commit has been created or authorized.

### 2. Next steps

1. Verify the R1 doc-only diff, links, whitespace, Git porcelain and research
   hash.
2. Present Section 14.1 to the user and wait for explicit approval.
3. On exact approval, append MRG1-A01, set state to `executing`, create the
   seven-section MR0-C00 packet and execute serially.
4. For each C00..C05: genuine RED where applicable, minimal GREEN, exact
   allowlist review, named staging, independent review pass, commit, push and
   ledger append.
5. Close MR0 only after final full/real gates and MRG1-S02.

Observable branches:

- user requires durable schema migration now: supersede D14, expand budget and
  reapprove before code;
- user changes research status to committed feature scope: new product
  decision and approval;
- approval wording changes but preserves scope: append exact wording to A01;
- any breaker in Section 13.7: stop, append evidence and request only the new
  authority needed.

### 3. Approved decisions

- MRG1-A00 remains documentation-only and completed.
- MRG1-D01..D13 remain active subject to D14's explicit MR0/MR1 split.
- MRG1-D14..D16 are proposed at R1 and become active only with MRG1-A01.
- MRG1-A01 is not yet granted.

### 4. Execution discipline

- Capability profile:
  `native-plan / serial / repo-artifact / native-session-poll`.
- Adapter: Codex desktop; artifact:
  `docs/orchestrated/vibecad-multi-runtime-g1.md`.
- Pre-approval write allowlist is exactly this artifact.
- Approved implementation allowlists, gates and breakers are Sections
  13.4–13.7.
- Preserve and poll original long-running sessions; never detach or relaunch.
- Stage only exact named files; never use `git add .` or `git add -A`.
- Push each accepted commit before the next.
- Recovery checks:

  ```text
  git status --short --branch
  git rev-parse HEAD
  git rev-parse @{upstream}
  git log -5 --oneline
  shasum -a 256 docs/MECHANICAL_DESIGN_VALIDATION_RESEARCH.md
  git diff --check
  ```

## 17. MRG1-A01 Authorization and Execution Re-evaluation

### 17.1 Authorization record

| ID | Time | Artifact revision | Decisions / packets | Exact user authorization | State |
|---|---|---|---|---|---|
| MRG1-A01 | 2026-07-26T01:37:10Z | MRG1-R1 | MRG1-D01..D16; MR0-C00..C05 | `批准  另外启动子 agent  编码过程 和 调研过程 都使用  sol max 的推理强度` | approved / executing |

MRG1-A01 binds the product-meaningful scope in Section 14.1 and additionally
requires subagents for both research and coding, with the host model selector
set to `gpt-5.6-sol` and reasoning effort `max`. This does not expand the file
allowlist, product scope, external authority, publication rights or circuit
breakers.

The earlier `awaiting user approval` statement in Section 14.2 is historical
and is superseded by this append-only authorization record. The same approval
must not be requested again unless an approved boundary changes or a circuit
breaker requires new authority.

### 17.2 Re-evaluated capability profile

Selected adapter: Codex desktop.

```text
approval: native-plan
delegation: spawn-send-wait
persistence: repo-artifact
process: native-session-poll
```

Permitted capability evidence categories:

- `live capability declarations`: `update_plan`, `spawn_agent`,
  `send_message`, `followup_task`, `wait_agent`, `exec_command`,
  `write_stdin` and `apply_patch` are declared in the current session; the
  user explicitly requested subagents, so the prior proactive-delegation
  restriction no longer prevents this bounded route.
- `observable behavior`: `update_plan`, `exec_command`, `write_stdin` and
  `apply_patch` have returned observable results in this session; subagent
  spawning and messaging are declared but have not yet been exercised at this
  re-evaluation point.
- `environment identity`: Codex desktop passively exposes the macOS workspace,
  repository root, current date/time and America/Los_Angeles timezone.
- `public configuration`: read-only Git configuration exposes branch
  `codex/agent-stage3` and upstream `origin/codex/agent-stage3`; no other
  relevant public configuration was observed.

No repository content, artifact approval, task packet, credential, token, key,
secret or private memory is classified as capability evidence.

Subagent model tier is `deep`; the adapter-local selection is explicitly
overridden by MRG1-A01 to `gpt-5.6-sol` with `max` reasoning for both research
and coding. Shared-file implementation remains serialized. The controller
retains approval traceability, artifact/ledger writes, gate verification,
distinct review, exact staging, commit and push.

### 17.3 Immediate packet order

1. `MR0-RSCH-01`: read-only, independent architecture and product-claim
   research against MRG1-R1; no writes.
2. `MR0-C00-IMPL`: documentation implementation excluding the controller-owned
   rolling artifact; no commit or push.
3. Controller verifies both returns, incorporates the artifact ledger, assigns
   a distinct review and closes MR0-C00.
4. Coding and review packets for MR0-C01..C05 proceed in dependency order;
   research packets may overlap only when read-only and file-independent.

## 18. MRG1-A02 Subagent Routing Amendment

| ID | Time | Artifact revision | Scope | Exact user authorization | State |
|---|---|---|---|---|---|
| MRG1-A02 | 2026-07-26T01:39:48Z | MRG1-R1 | subagent model/reasoning routing and gate delegation only | `后续  出一些门禁执行也启动 subagent  纯机械验证可用 gpt-5.6-terra / medium，常规编码可用 sol / high，关键架构与对抗复核用 sol / max` | active; supersedes only A01's uniform model routing |

MRG1-A02 does not alter the approved product scope, allowlist, commit budget,
gates or external authority. It refines packet routing:

```text
mechanical gate execution: gpt-5.6-terra / medium
routine coding:            gpt-5.6-sol / high
critical architecture:     gpt-5.6-sol / max
adversarial review:        gpt-5.6-sol / max
```

The already-running MR0-C00 documentation packet may complete at
`gpt-5.6-sol / max`; interrupting it is not required. Subsequent packets use
the refined route. Gate subagents execute exact commands and return raw
evidence without editing, staging, committing or pushing. The controller
independently verifies command identity, exit status, counts, repository
state, exact staging, commit and push.

## 19. MR0-C00 Execution Ledger and Recovery Snapshot MRG1-S02

### MR0-C00 outcome before commit

At `2026-07-26T02:02:30Z`, MR0-C00 implementation and review are complete and
ready for exact staging:

- implementation subagent: `gpt-5.6-sol / max`;
- independent architecture/product research and settled-diff review:
  `gpt-5.6-sol / max`;
- mechanical gate and targeted recheck: `gpt-5.6-terra / medium`;
- exact changed scope: the five tracked canonical product/architecture docs,
  this rolling artifact and the previously untracked mechanical research doc;
- executable/config changes: `0`;
- `git diff --check`: exit `0`;
- scoped relative Markdown links: `21` checked, `0` broken;
- research SHA-256:
  `25b849b181cd7315cb148011bc82a7e829a4c80192cf0e00c1574ed0e30751f0`;
- independent review: Critical `0`, Major `0`, Medium `0`, Minor `0`;
- FreeCAD remains the sole connected adapter; C00 is documentation-only and
  does not claim MR0 foundation readiness.

Two review findings in the controller-owned artifact were corrected before
PASS: the pre-approval statement is now historical and superseded by A01, and
the trusted parent compatibility adapter is distinguished from the
store/lease-free child Worker/provider protocol.

| Entry ID | Decision / approval | Commit / push | Gate evidence | Residual | Snapshot | State |
|---|---|---|---|---|---|---|
| MR0-C00-E01 | D01..D16; A01; A02 | `not-created`; exact staging next | C00-G0: diff check 0; links 21/0; allowlist 7/7; research hash matched; review 0/0/0/0 | MRG1-RES-10 | MRG1-S02 | gated / ready-to-commit |

MRG1-RES-10: `README.md` retains the pre-MR0 milestone order at the audited
baseline but is outside the approved C00 allowlist. It does not invalidate the
named canonical-document gate. Closure requires a separately approved
documentation update; until then, do not claim repository-wide documentation
consistency.

### 1. Completed milestones

- Repository remains on `codex/agent-stage3` at pre-C00
  `04045da62822af964e04140b43620469d2841c61`.
- MRG1-A01 approved MRG1-R1; A02 refined subagent routing.
- C00 canonical documentation, research adoption, G0 gates, independent review
  and targeted recheck are complete.
- No source, test, package, public schema, version or runtime behavior changed.
- C00 commit and push have not yet occurred at this snapshot.

### 2. Next steps

1. Stage exactly the seven named C00 paths.
2. Inspect cached name-status, stat and diff; rerun `git diff --cached --check`.
3. Commit `docs(architecture): define multi-runtime CAD boundary`.
4. Push `codex/agent-stage3` immediately and verify local/upstream equality.
5. Append the exact C00 hash/push state in the next rolling artifact entry,
   then issue MR0-C01.

Observable branches:

- staged path outside C00 allowlist: unstage only that exact path, preserve the
  workspace and stop;
- commit hook/gate red: preserve output, do not bypass;
- push rejection or remote advance: inspect and reconcile without force;
- research hash drift: stop before commit.

### 3. Approved decisions

- MRG1-D01..D16 are active under MRG1-A01.
- MRG1-A02 controls subagent routing only.
- C00 adopts the mechanical document as direction research, not committed
  feature scope.
- D14 keeps durable FCStd/STEP migration in MR1.

### 4. Execution discipline

- Capability profile:
  `native-plan / spawn-send-wait / repo-artifact / native-session-poll`.
- C00 exact allowlist is Section 13.4; no broad staging is permitted.
- Stage named files only; commit and push are controller-owned.
- Active breakers and residual rules are Sections 13.7 and 13.9.
- Recovery commands remain:

  ```text
  git status --short --branch
  git rev-parse HEAD
  git rev-parse @{upstream}
  shasum -a 256 docs/MECHANICAL_DESIGN_VALIDATION_RESEARCH.md
  git diff --check
  ```

## 20. MR0-C00 Gate-Red Record and Corrective Packet

### MR0-C00-E02 — staged whitespace gate red

The exact named staging exposed a condition that the earlier worktree check
could not observe while the research file was untracked:

```text
git diff --cached --check
exit 2
docs/MECHANICAL_DESIGN_VALIDATION_RESEARCH.md:3: trailing whitespace
docs/MECHANICAL_DESIGN_VALIDATION_RESEARCH.md:4: trailing whitespace
```

Both lines use two trailing spaces as implicit Markdown hard breaks. The
research content and approved noncommitment status are unchanged, but this is
an unpredicted G0 red. It is preserved here and is not waived or relabeled.

| Entry ID | Decision / approval | Commit / push | Gate evidence | Residual | Snapshot | State |
|---|---|---|---|---|---|---|
| MR0-C00-E02 | D16; A01; A02 | `not-created`; staged attempt retained | C00-G0 staged diff check exit 2 at research lines 3–4 | none; exact format defect | MRG1-S02 | blocked / corrective packet required |

### MR0-C00-FIX01 — explicit Markdown formatting

1. **Authorization:** MRG1-R1/D16 under A01 permits link/format corrections
   without changing the report's product status or feature scope. No new
   product authority is required.
2. **Workspace anchor:** same C00 anchor and exact allowlist; correction may
   modify only the research document and this controller-owned artifact.
3. **Context:** replace two invisible trailing-space hard breaks with explicit
   blockquote paragraph separators; do not alter research prose.
4. **Steps and gates:** apply the two-line mechanical format change, record
   old/new hashes, restage those two named files, rerun exact staged allowlist,
   `git diff --cached --check`, relative links, overclaim search and
   independent mechanical recheck.
5. **Execution discipline:** controller applies the narrow correction;
   `gpt-5.6-terra / medium` reruns mechanical gates. Stop on any semantic
   research diff or further red.
6. **Delivery boundary:** no commit or push until the corrective gate passes.
7. **Final report:** exact diff, hashes, commands, exit status, path list and
   final staged state.

### MR0-C00-FIX01 GREEN

- new research SHA-256:
  `2b5df222751f6fa6ecdb70a8671ce904ae7e95f02799b92af6a9fac025e4255e`;
- mechanical reconstruction of only the two explicit blockquote separators
  reproduces the old SHA-256
  `25b849b181cd7315cb148011bc82a7e829a4c80192cf0e00c1574ed0e30751f0`;
- research prose and noncommitment status are unchanged;
- `git diff --cached --check`: exit `0`;
- exact staged allowlist: `7` paths;
- relative Markdown links: `21` checked, `0` broken;
- unstaged paths: `0`; untracked paths: `0`;
- mechanical corrective gate: PASS.

| Entry ID | Decision / approval | Commit / push | Gate evidence | Residual | Snapshot | State |
|---|---|---|---|---|---|---|
| MR0-C00-FIX01-E03 | D16; A01; A02 | `not-created`; commit next | exact format reconstruction; staged diff check 0; links 21/0; allowlist 7/7; research status unchanged | none | MRG1-S02 | green / final adversarial recheck |

## 21. MR0-C00 Finalization and MR0-C01 Task Packet

### 21.1 MR0-C00 accepted commit

At `2026-07-26T02:13:05Z`, the controller verified and pushed the accepted
documentation boundary:

- commit:
  `6cc1876c8a76c9e3498262c78a0ad2c4ec6ddf6c`;
- subject: `docs(architecture): define multi-runtime CAD boundary`;
- push: `origin/codex/agent-stage3`, success;
- local `HEAD` and upstream:
  `6cc1876c8a76c9e3498262c78a0ad2c4ec6ddf6c`;
- post-push worktree: clean;
- exact accepted scope: seven C00 paths, including the formerly untracked
  mechanical validation research document;
- final independent review: Critical `0`, Major `0`, Medium `0`, Minor `0`;
- residual: MRG1-RES-10 remains open and does not authorize a `README.md`
  change in MR0.

| Entry ID | Decision / approval | Commit / push | Gate evidence | Residual | Snapshot | State |
|---|---|---|---|---|---|---|
| MR0-C00-E04 | D01..D16; A01; A02 | `6cc1876c8a76c9e3498262c78a0ad2c4ec6ddf6c`; pushed | corrective gate PASS; architecture/adversarial review 0/0/0/0; local/upstream equal | MRG1-RES-10 | MRG1-S03 | accepted / closed |

### 21.2 MR0-C01 seven-section implementation packet

#### 1. Authorization

MRG1-A01 authorizes MRG1-R1 and MR0-C01. MRG1-A02 routes routine coding to
`gpt-5.6-sol / high`, architecture/adversarial review to
`gpt-5.6-sol / max`, and mechanical gate execution to
`gpt-5.6-terra / medium`. No new product approval is required.

#### 2. Workspace anchor and exact write scope

Start from clean pushed commit
`6cc1876c8a76c9e3498262c78a0ad2c4ec6ddf6c` on
`codex/agent-stage3`. The implementation subagent may write only:

```text
src/vibecad/runtime/contracts.py
src/vibecad/runtime/registry.py
tests/test_runtime_contracts.py
tests/test_runtime_registry.py
```

`src/vibecad/runtime/__init__.py` remains in the approved commit allowlist but
should stay unchanged unless a test-proven import requirement appears. This
avoids an unnecessary additive package surface. The rolling artifact is
controller-owned and must not be edited by subagents.

#### 3. Context and required behavior

Build the domain-neutral foundation only: immutable runtime identity and
descriptor, declared capabilities, invocation ownership/correlation with
sealed input, budget/deadline, lifecycle status/control port, immutable result
artifacts/provenance/diagnostics/evidence, and a deterministic registry.
The common layer must not import CAD, FreeCAD, Qt, FEA, reconstruction,
application or public-tool modules. It must not change Task Kernel, durable
revision layout, public schemas, public operation projection or the 28-tool
surface.

#### 4. Test-first steps and gates

1. Create tests that specify immutability, strict validation, stable ordering,
   duplicate rejection, budget/deadline behavior, cancellation/reconciliation
   control and immutable artifact/provenance results.
2. Run the approved focused command before implementation and preserve a
   genuine RED caused by the deliberately absent contracts/registry.
3. Implement only enough generic code to satisfy the contract.
4. Rerun:

   ```text
   PYTHONPATH=src .venv/bin/python -m pytest -q \
     tests/test_runtime_contracts.py tests/test_runtime_registry.py
   ```

5. Run scoped Ruff, `git diff --check`, exact allowlist and a common-layer
   forbidden-import search. A distinct `sol / max` reviewer checks architecture
   and authority boundaries; a `terra / medium` gate agent reruns exact
   mechanical commands.

#### 5. Execution discipline and breakers

Implementation is serialized in the shared worktree. Subagents do not stage,
commit, push or edit this artifact. The controller owns the ledger, exact named
staging, commit and immediate push. Stop on any required change outside the
C01 allowlist, public/API surface expansion, persistence migration, Task
Kernel edit, non-determinism, real external runtime requirement, or gate red
that cannot be resolved by a narrow C01 correction.

#### 6. Delivery boundary

The intended commit is exactly:

```text
feat(runtime): add runtime capability contracts
```

It may contain only the four implementation/test paths above plus this rolling
artifact; `src/vibecad/runtime/__init__.py` may be added only if the recorded
test evidence makes it necessary. No commit or push occurs until focused
GREEN, purity evidence, independent review and mechanical recheck all pass.

#### 7. Required final report

Return the RED cause and exit status, GREEN commands and counts, exact files
changed, contract decisions, forbidden-import result, scoped Ruff/diff status,
review findings by severity, residuals and whether `runtime/__init__.py`
remained unchanged. The controller then records the accepted hash/push state
before issuing MR0-C02.

## 22. MR0-C01 Test-First Ledger, Review Red and Corrective Packet

### 22.1 Initial RED and focused GREEN

The `gpt-5.6-sol / high` implementation subagent obeyed the exact write scope
and test-first order:

```text
PYTHONPATH=src .venv/bin/python -m pytest -q \
  tests/test_runtime_contracts.py tests/test_runtime_registry.py
```

- genuine RED before source implementation: exit `2`; both test modules
  failed collection with
  `ModuleNotFoundError: No module named 'vibecad.runtime.contracts'`;
- the environment and test parsing were healthy, and the missing modules were
  deliberately absent C01 targets;
- initial GREEN after implementation: exit `0`, `18 passed in 0.10s`;
- scoped Ruff lint and format check: PASS;
- source forbidden-import search: `0` matches;
- `git diff --check` and explicit trailing-whitespace scan: PASS;
- exact implementation paths: the two new runtime modules and two new test
  modules; `src/vibecad/runtime/__init__.py` remained unchanged;
- no stage, commit or push occurred.

This focused GREEN was necessary but not sufficient for acceptance.

### 22.2 MR0-C01-E02 — independent architecture/adversarial review red

At `2026-07-26T02:31:19Z`, the distinct `gpt-5.6-sol / max` review returned
FAIL with Critical `0`, Major `1`, Medium `4`, Minor `0`:

1. **Major:** `RuntimeControlPort` exposes only status, cancel and reconcile,
   while approved D03 requires start, status, cancel, health and reconcile.
   An immutable invocation value does not grant adapter start authority.
2. **Medium:** six mapping-root fields rely on `assert`; under `python -O`,
   descriptor/artifact metadata, invocation payload and result output probes
   accepted list roots as tuples.
3. **Medium:** a descriptor accepts execution profile `Headless GUI`, while an
   invocation rejects it, so discovery can advertise an unusable route.
4. **Medium:** a result may return artifacts with no provenance, contrary to
   the approved provenance-bound artifact boundary.
5. **Medium:** frozen JSON limits depth and numeric range but not traversal
   nodes, collection width or string/key bytes; probes accepted a
   100,000-item sequence and a one-million-character string.

Registry duplicate/unknown behavior, exact-version lookup, deterministic
lexical ordering, domain purity, file scope and the unchanged package
`__init__.py` otherwise passed review.

Controller probes independently reproduced the first three findings:

```text
python -O mapping-root probes:
descriptor_metadata ACCEPTED tuple
artifact_metadata   ACCEPTED tuple
invocation_payload  ACCEPTED tuple
result_output       ACCEPTED tuple

RuntimeControlPort methods:
cancel, get_status, reconcile
```

| Entry ID | Decision / approval | Commit / push | Gate evidence | Residual | Snapshot | State |
|---|---|---|---|---|---|---|
| MR0-C01-E01 | D01..D16; A01; A02 | `not-created` | genuine import RED exit 2; initial focused GREEN 18; Ruff/diff/purity PASS | none | MRG1-S03 | implemented / review pending |
| MR0-C01-E02 | D01..D16; A01; A02 | `not-created`; forbidden | independent review 0/1/4/0; optimized-mode and protocol probes reproduced | none; five exact defects | MRG1-S03 | blocked / corrective packet required |

The review red is preserved and is not waived or relabeled.

### 22.3 MR0-C01-FIX01 — lifecycle, validation and bounded-freeze correction

#### 1. Authorization

MRG1-D03, D06 and the C01 strict/immutable/budget gates already require these
behaviors under MRG1-A01. MRG1-A02 keeps routine correction coding at
`gpt-5.6-sol / high`, adversarial re-review at `gpt-5.6-sol / max` and final
mechanical gates at `gpt-5.6-terra / medium`. No product scope expands.

#### 2. Workspace anchor and write scope

The pushed anchor remains
`6cc1876c8a76c9e3498262c78a0ad2c4ec6ddf6c`. The correction may change only
the existing four C01 source/test paths; this artifact remains
controller-owned. `src/vibecad/runtime/__init__.py` must remain unchanged.

#### 3. Exact corrective behavior

- add Protocol-only `start(RuntimeInvocation)` plus immutable, domain-neutral
  runtime health state/snapshot and `health(RuntimeIdentity)`;
- replace every assertion-based mapping-root check with explicit stable
  rejection that remains active under optimized Python;
- use one strict execution-profile grammar for descriptor and invocation;
- require matching provenance whenever a result contains artifacts;
- bound frozen JSON depth, per-container width, total traversed nodes and
  UTF-8 bytes for strings/keys, rejecting cycles and hostile containers
  deterministically before unbounded materialization.

#### 4. Test-first steps and gates

Add negative and lifecycle tests first, then run the focused command and
preserve a corrective RED attributable to the five findings. Implement the
narrow correction, rerun the exact focused suite, scoped Ruff/format,
`git diff --check`, optimized-mode mapping probes, bounded-container probes and
forbidden-import search.

#### 5. Execution discipline and breakers

The implementation subagent remains the sole source/test writer. It must not
edit the artifact, stage, commit or push. Stop if correction needs a public
schema, CAD/domain import, Task Kernel/persistence edit, clock/random source,
third-party dependency or path outside C01.

#### 6. Delivery boundary

The original C01 commit subject and exact commit budget remain unchanged. No
commit occurs until all five review findings have regression coverage, the
focused suite is GREEN and the same `sol / max` reviewer plus an independent
`terra / medium` gate agent both return PASS.

#### 7. Required final report

Return corrective RED failures/count, final GREEN count, exact contract
changes and bounds, optimized-mode evidence, hostile/cyclic evidence,
Ruff/diff/purity results, exact paths and any residual. The controller records
the old red, correction and re-review separately.

### 22.4 MR0-C01-FIX01 result and MR0-C01-E03 re-review red

FIX01 preserved a second test-first transition:

- corrective RED before source changes: exit `1`,
  `6 failed, 19 passed in 3.91s`;
- all five E02 findings were represented, including a controlled two-second
  timeout for the previously unbounded infinite JSON `Sequence`;
- no timed-out child process remained;
- focused GREEN: exit `0`, `25 passed in 2.44s`;
- optimized/hostile targeted subset: `4 passed`;
- scoped Ruff/format, diff/whitespace and forbidden-import gates: PASS;
- implemented bounds: depth `32`, `1,024` items per JSON container, `8,192`
  nodes per root, and `65,536` UTF-8 bytes per individual string/key;
- lifecycle, optimized-root, profile-grammar and artifact-provenance findings
  were closed.

The `gpt-5.6-sol / max` re-review at `2026-07-26T02:44:33Z` nevertheless
returned FAIL with Critical `0`, Major `0`, Medium `3`, Minor `0`:

1. the UTF-8 limit is per string/key, not aggregate; `1,024` references to one
   individually valid `65,536`-byte string produced a passing
   `67,108,864`-byte logical value;
2. `_typed_tuple` and `_text_tuple` still exhaust arbitrary caller
   `Sequence` objects before validation; controlled endless profile and
   capability sequences timed out;
3. `RuntimeRegistry` still calls `tuple()` on a public `Iterable`; a controlled
   endless generator timed out.

All timeout subprocesses were terminated. The controller independently
reproduced the endless profile sequence and registry generator with one-second
timeouts. No stage, commit or push has occurred.

| Entry ID | Decision / approval | Commit / push | Gate evidence | Residual | Snapshot | State |
|---|---|---|---|---|---|---|
| MR0-C01-FIX01-E03 | D03; D06; A01; A02 | `not-created` | corrective RED 6/19; GREEN 25; targeted 4; Ruff/diff/purity PASS | none | MRG1-S03 | corrected / re-review pending |
| MR0-C01-E03 | D03; D06; A01; A02 | `not-created`; forbidden | re-review 0/0/3/0; 67,108,864 logical bytes; two controlled iterable timeouts | none; three boundedness defects | MRG1-S03 | blocked / FIX02 required |

The second review red is preserved and is not waived or folded into FIX01.

### 22.5 MR0-C01-FIX02 — cumulative and iterable bounds

#### 1. Authorization

C01's approved strict, immutable and resource-bounded input contract already
covers these three defects under D03/A01. A02 routes the narrow correction to
`gpt-5.6-sol / high`, re-review to `gpt-5.6-sol / max` and final mechanical
gate execution to `gpt-5.6-terra / medium`.

#### 2. Workspace anchor and exact scope

The pushed anchor remains
`6cc1876c8a76c9e3498262c78a0ad2c4ec6ddf6c`. Only the same four C01
source/test paths may change; the artifact is controller-owned and
`src/vibecad/runtime/__init__.py` remains unchanged.

#### 3. Exact corrective behavior

- add one cumulative UTF-8 byte counter to the shared per-root freeze budget
  and count every logical string value and mapping key, including repeated
  references;
- replace unbounded tuple/text `Sequence` conversion with incremental
  `limit + 1` enumeration that ignores untrusted length hints and rejects
  excess, endless and failing iterators deterministically;
- apply an explicit finite descriptor count to registry enumeration using the
  same incremental pattern before materialization;
- retain all FIX01 depth, width, node, individual string/key, cycle and
  provenance/lifecycle behavior.

#### 4. Test-first steps and gates

Add aggregate-byte, endless typed/text sequence and endless registry-generator
tests before source edits. Run the focused suite and preserve a FIX02 RED.
Implement narrowly, then rerun the exact focused suite, controlled timeout
probes, scoped Ruff/format, optimized-root tests, `git diff --check` and
forbidden-import search.

#### 5. Execution discipline and breakers

The coding subagent remains sole source/test writer and may not stage, commit,
push or edit this artifact. No clock, process-control implementation,
third-party dependency, domain import or path expansion is allowed. Every
bounded iterator must stop after at most its declared limit plus one item.

#### 6. Delivery boundary

The original C01 commit subject and six-commit stage budget remain unchanged.
Commit is forbidden until FIX02 GREEN, a third settled-diff `sol / max`
review PASS and independent `terra / medium` mechanical PASS.

#### 7. Required final report

Return the FIX02 RED count, final GREEN count, cumulative byte and collection
bounds, controlled timeout outcomes, exact paths, Ruff/diff/purity evidence,
process cleanup and residuals.

### 22.6 MR0-C01-FIX02 GREEN and accepted pre-staging gates

FIX02 completed within the same four source/test paths:

- genuine FIX02 RED before source edits: exit `1`,
  `6 failed, 25 passed in 6.58s`;
- controlled failures covered aggregate UTF-8, endless capability/profile
  sequences, explicit contract collection size, explicit registry size and
  endless registry enumeration; all subprocesses were cleaned;
- final focused GREEN: exit `0`, `31 passed`;
- final bounds:
  - JSON depth `32`;
  - per JSON container `1,024` items;
  - per frozen root `8,192` nodes;
  - per string/key `65,536` UTF-8 bytes;
  - cumulative logical string/key bytes per frozen root `1,048,576`;
  - every non-JSON contract `Sequence` `1,024` items;
  - runtime registry `256` descriptors;
- all collection and registry snapshots enumerate incrementally and reject
  after at most `limit + 1` reads without trusting `len()` or length hints.

The third settled-diff `gpt-5.6-sol / max` review returned PASS:
Critical `0`, Major `0`, Medium `0`, Minor `0`. It verified exact boundary
acceptance/rejection, repeated key/value byte accounting, iterator read
counts/failures, all prior lifecycle/provenance/optimized-mode closures,
authority purity and an unchanged package `__init__.py`.

The independent `gpt-5.6-terra / medium` pre-staging mechanical gate also
returned PASS with no deviations:

```text
focused:                         31 passed
contract hostile/bounds subset:  8 passed, 17 deselected
registry bounds subset:          2 passed, 4 deselected
Ruff lint / format:              PASS / PASS
actual / allowed paths:          5 / 5
out of allowlist:                0
AST purity / source asserts:     0 / 0
trailing whitespace / EOF:       0 / 0
leftover probe processes:         0
```

Reviewed SHA-256 values:

```text
9dd1948d9da86c9ca6f8e1ba8e7fdab44d1f4dcc6dfe2927bf66bf3baa7c9c1a  src/vibecad/runtime/contracts.py
d4c554e591afd5055a829b91aa62250f661ac26e5a7051602cc433b80248c382  src/vibecad/runtime/registry.py
2998f34ccd48be6ffefebb64a9a5504b3c856ff7c4d579c51e636603f7c077d0  tests/test_runtime_contracts.py
ad85d1bd878fcd9e45b347b4d14edc193af56a8891561c7d50d28f371562e3fe  tests/test_runtime_registry.py
```

`src/vibecad/runtime/__init__.py` remains byte-identical to HEAD with SHA-256
`217184fec30d06cbe7f79f0c54589462f2ef1f23afb4ec75c36d37e02b86dee1`.
MRG1-RES-02 remains open until C04 adds reusable conformance and authority
negative tests; C01 supplies the generic contracts/registry portion only.

After exact named staging, the same `gpt-5.6-terra / medium` agent returned a
second mechanical PASS with no deviations:

```text
cached paths:                     5 exact
unstaged / untracked:             0 / 0
git diff --cached --check:        exit 0
focused suite:                    31 passed
Ruff lint / format:               PASS / PASS
artifact headings:               Sections 1..22 consecutive; Section 22 terminal
prohibited imports/asserts:       0
process leaks:                    0
```

| Entry ID | Decision / approval | Commit / push | Gate evidence | Residual | Snapshot | State |
|---|---|---|---|---|---|---|
| MR0-C01-FIX02-E04 | D03; D06; A01; A02 | `not-created`; commit next | FIX02 RED 6/25; GREEN 31; sol/max review 0/0/0/0; terra/medium pre/post-stage PASS; cached diff check 0; hashes matched | MRG1-RES-02 until C04 | MRG1-S03 | gated / ready-to-commit |

## 23. MR0-C01 Finalization and MR0-C02 Task Packet

### 23.1 MR0-C01 accepted commit

At `2026-07-26T03:12:42Z`, the controller verified and pushed the accepted
generic runtime foundation:

- commit:
  `07c6d6cd0260dcce41711a4a92d47132460571db`;
- subject: `feat(runtime): add runtime capability contracts`;
- push: `origin/codex/agent-stage3`, success;
- local `HEAD` and upstream:
  `07c6d6cd0260dcce41711a4a92d47132460571db`;
- post-push worktree: clean;
- exact accepted scope: two generic runtime modules, two focused test modules
  and this rolling artifact; `src/vibecad/runtime/__init__.py` unchanged;
- accepted gates: focused `31 passed`, scoped Ruff PASS, `sol / max` review
  Critical/Major/Medium/Minor `0/0/0/0`, `terra / medium` post-staging PASS;
- MRG1-RES-02 remains open until C04 supplies reusable conformance and
  authority-negative tests.

| Entry ID | Decision / approval | Commit / push | Gate evidence | Residual | Snapshot | State |
|---|---|---|---|---|---|---|
| MR0-C01-E05 | D01..D16; A01; A02 | `07c6d6cd0260dcce41711a4a92d47132460571db`; pushed | RED/FIX01/FIX02 preserved; GREEN 31; review 0/0/0/0; mechanical PASS; local/upstream equal | MRG1-RES-02 until C04 | MRG1-S04 | accepted / closed |

### 23.2 MR0-C02 architecture audit

The read-only `gpt-5.6-sol / max` C02 design audit returned PASS with no
breaker. It confirmed:

- the existing revision-bound, backend-neutral `SelectorV1` is the semantic
  selector authority and must be wrapped without changing its wire schema;
- the five capability outcomes require distinct immutable sum-type variants,
  not one optional-field record;
- a narrow adapter selection Protocol declares only a runtime descriptor,
  `generation_lost`, `terminate_generation()` and `close_generation()`; it
  does not inherit `RuntimeControlPort` or invent a second invocation state
  machine;
- C03 can construct one `WorkerCadExecutionPort`, register it, select it
  through the router for internal capability
  `authoring.execute_program@1`, assert the selected object is the same
  nominal worker and return it unchanged;
- `src/vibecad/interaction/__init__.py` should remain byte-identical with
  SHA-256
  `f1e9b6e50b2042c09dff60d024a6fbf53ee09f2507b6b66dfa0423de9ae776a5`.

### 23.3 MR0-C02 seven-section implementation packet

#### 1. Authorization

MRG1-A01 authorizes C02 under D01–D15. MRG1-A02 routes routine coding to
`gpt-5.6-sol / high`, architecture/adversarial review to
`gpt-5.6-sol / max`, and mechanical gates to
`gpt-5.6-terra / medium`. The packet introduces internal CAD-domain contracts
only and does not change public product support.

#### 2. Workspace anchor and exact write scope

Start from clean pushed commit
`07c6d6cd0260dcce41711a4a92d47132460571db`. The implementation subagent may
write only:

```text
src/vibecad/interaction/cad_runtime.py
tests/test_cad_runtime.py
```

The rolling artifact is controller-owned.
`src/vibecad/interaction/__init__.py` remains in the approved commit allowlist
but must stay unchanged unless a test-proven import requirement appears; in
that case stop instead of editing it.

#### 3. Context and required internal design

Implement a backend-neutral internal CAD runtime layer:

- `CadRuntimeIdentity` wraps an exact generic `RuntimeIdentity` whose family
  is `cad`;
- `CadRuntimeExtension` makes non-portable requests explicit and
  runtime-qualified;
- five frozen decision variants represent native execution, disclosed
  semantic mapping, explicit non-executable approximation, mutation-free
  unsupported rejection and namespaced runtime extension; only native,
  mapping and extension are executable;
- native requires the exact requested declared capability; mapping and
  extension selections must also be declared; approximation/unsupported carry
  no executable selection, and an unknown request becomes unsupported before
  any adapter lifecycle call;
- `CadArtifactProfile` contains runtime-qualified, versioned role/kind/media
  declarations with exactly one native model; validating a concrete generic
  `RuntimeArtifact` requires exact runtime, kind and media type. It contains no
  path, fixed FCStd/STEP filename, store or durable schema field;
- `CadSelectorEnvelope` contains the existing revalidated `SelectorV1` as
  mandatory semantic authority plus an optional exact-runtime/revision
  `NativeLocator`; dropping the native locator preserves semantic identity,
  and a bare ephemeral locator can never stand alone;
- `CadRuntimeDescriptor`, a bounded deterministic adapter registry, router and
  `CadDomainService` provide exact-version planning and adapter selection for
  two independently registered fixture identities;
- `CadRuntimeAdapter` is a structural selection/compatibility Protocol with
  only `runtime_descriptor`, `generation_lost`,
  `terminate_generation()` and `close_generation()`. It exposes no store,
  lease, Task, revision, review, Accept/Reject, commit, HEAD or public-tool
  authority and remains separate from generic `RuntimeControlPort`;
- define internal routing capability
  `CAD_EXECUTE_PROGRAM_V1 =
  RuntimeCapability(name="authoring.execute_program", version=1)` for C03
  composition. This is not a seventh public operation or a portability claim.

Permitted non-stdlib imports are the generic runtime contracts/registry and
the existing backend-neutral `SelectorV1` contract. Do not import
`interaction.cad`, application, workflow, worker, FreeCAD, Qt, FEA,
reconstruction, FCStd/STEP layout or public server/tool modules.

#### 4. Test-first steps and gates

1. Create `tests/test_cad_runtime.py` first and syntax-check it.
2. Run the exact approved command before implementation:

   ```text
   PYTHONPATH=src .venv/bin/python -m pytest -q \
     tests/test_runtime_contracts.py tests/test_runtime_registry.py \
     tests/test_cad_runtime.py
   ```

   Preserve collection exit `2` caused only by the deliberately absent
   `vibecad.interaction.cad_runtime` module.
3. Implement the narrow module and rerun the same command to GREEN.
4. Tests must cover all five exact decision types/invariants, undeclared
   capabilities, mutation-free approximation/unsupported routing with zero
   adapter calls, explicit extension identity, duplicate/unknown/exact-version
   registration, reversed-order fixture identities, artifact role/runtime/
   kind/media mismatch, mandatory semantic selectors, runtime/revision/native
   mismatch, native loss retaining semantic identity, bounded hostile/endless
   inputs, forbidden authority names and AST import purity.
5. Run scoped Ruff/format, `git diff --check`, exact allowlist and
   `interaction/__init__.py` hash checks. Distinct `sol / max` and
   `terra / medium` agents must both PASS.

#### 5. Execution discipline and breakers

The implementation subagent is the sole source/test writer and may not edit
the artifact, stage, commit or push. Stop on any required edit to
`interaction/cad.py`, `interaction/__init__.py`, application/project, worker,
generic runtime C01, revisions/store/schema/layout, public `SelectorV1`, six
operations or 28 tools. Also stop on any adapter authority expansion,
FreeCAD-specific import/field, product-support claim for fixture identities,
unsupported/approximation path that returns or invokes an adapter, or
unbounded public iterable.

#### 6. Delivery boundary

The intended commit is exactly:

```text
feat(cad): add backend-neutral CAD runtime port
```

It may contain only `cad_runtime.py`, `test_cad_runtime.py` and this artifact.
No commit or push occurs until focused GREEN, all five decision branches,
authority/purity evidence, independent review and mechanical recheck pass.

#### 7. Required final report

Return the exact RED cause/exit, GREEN command/count, exact files, value/API
decisions, branch and mutation counters, selector/artifact negatives, iterable
bounds, purity/authority results, Ruff/diff/hash evidence, review findings,
residuals and whether `interaction/__init__.py` remained unchanged.

## 24. MR0-C02 Test-First Ledger, Review Red and Corrective Packet

### 24.1 Initial RED, design correction and focused GREEN

The `gpt-5.6-sol / high` implementation subagent preserved two test-first
transitions within the exact two-file write scope:

1. before source implementation, the approved exact command exited `2`; the
   sole collection error was the deliberately absent
   `vibecad.interaction.cad_runtime` module, while existing C01 tests collected
   normally;
2. after a first GREEN, three architecture checks were turned into tests
   before correction: explicit extension requests, all six approved artifact
   roles and immutable registry-admission descriptors. That corrective run
   exited `1` with `6 failed, 49 passed`;
3. the settled focused command then returned `55 passed`:
   `31` generic runtime cases and `24` C02 cases.

The two new files only were changed. Scoped Ruff/format, AST import purity,
source-assert scan, whitespace/diff checks and hostile-iterable process cleanup
passed. `src/vibecad/interaction/__init__.py` retained SHA-256
`f1e9b6e50b2042c09dff60d024a6fbf53ee09f2507b6b66dfa0423de9ae776a5`.
No stage, commit or push occurred.

### 24.2 MR0-C02-E02 — independent architecture/adversarial review red

At `2026-07-26T03:37:21Z`, the distinct `gpt-5.6-sol / max` review returned
FAIL with Critical `0`, Major `1`, Medium `3`, Minor `0`:

1. **Major — compound authority names bypass admission.** Exact-name filtering
   rejects `commit` but admitted `commit_revision`, `advance_head`,
   `accept_draft`, `reject_task` and `review_task`, allowing a routed adapter
   to expose second-authority methods contrary to D01.
2. **Medium — cross-runtime unsupported extension rules are admitted but
   unreachable.** A descriptor for runtime A accepted an unsupported decision
   whose extension request belongs to runtime B; planning then rejects the
   request before consulting the configured rule.
3. **Medium — singular artifact roles have no cardinality bound.** Profiles
   admitted multiple semantic observations, selector mappings and provenance
   declarations with different kinds.
4. **Medium — generation hook signatures are unchecked.** Protocol runtime
   membership admitted adapters whose `terminate_generation(reason)` and
   `close_generation(force)` require arguments, while application call sites
   invoke both with zero arguments.

The reviewer independently reproduced all four. Focused `55`, Ruff/format,
imports, selectors, decision branches, exact routing, mutation-free rejection,
bounds and scope otherwise passed.

Reviewed hashes:

```text
e6b4704bcbb8d674ef49a44229d3772f4a31555b2c905ab5fca81eec73fcb1ac  src/vibecad/interaction/cad_runtime.py
cbd0f3032d851425bea2e70e984e31c679959a5ae90d9d5f7c502452e0d628bb  tests/test_cad_runtime.py
```

| Entry ID | Decision / approval | Commit / push | Gate evidence | Residual | Snapshot | State |
|---|---|---|---|---|---|---|
| MR0-C02-E01 | D01..D15; A01; A02 | `not-created` | genuine absent-module RED 2; design RED 6/49; focused GREEN 55; Ruff/purity PASS | none | MRG1-S04 | implemented / review pending |
| MR0-C02-E02 | D01; D03..D07; A01; A02 | `not-created`; forbidden | review 0/1/3/0; four controller/reviewer probes reproduced | none; four exact defects | MRG1-S04 | blocked / FIX01 required |

The review red is preserved and is not waived or relabeled.

### 24.3 MR0-C02-FIX01 — authority, request, artifact and hook correction

#### 1. Authorization

D01, D03, D05 and D06 plus the approved C02 authority and compatibility gates
already require these behaviors under A01. A02 keeps narrow correction coding
at `gpt-5.6-sol / high`, re-review at `gpt-5.6-sol / max` and mechanical gates
at `gpt-5.6-terra / medium`. No product scope expands.

#### 2. Workspace anchor and exact scope

The pushed anchor remains
`07c6d6cd0260dcce41711a4a92d47132460571db`. FIX01 may change only the same
new `cad_runtime.py` and `test_cad_runtime.py`; this artifact remains
controller-owned and `interaction/__init__.py` must remain unchanged.

#### 3. Exact corrective behavior

- inspect public adapter names by normalized identifier tokens and reject
  commit-, HEAD-, Accept-, Reject- and review-authority names including
  compound snake/camel forms, while allowing trusted parent compatibility
  names such as `open_revision`, private `_store` and ordinary CAD methods;
- require every runtime-qualified extension request, including an unsupported
  decision, to match its decision and descriptor runtime;
- permit multiple exchange/evidence declarations but at most one semantic
  observation, selector mapping and provenance declaration, in addition to
  exactly one native model;
- validate without invoking that the bound `terminate_generation` and
  `close_generation` hooks are callable with zero arguments; reject required
  positional/keyword-only parameters before registry admission.

#### 4. Test-first steps and gates

Add regression tests for compound/camel authority names, Worker-shaped allowed
methods/private fields, cross-runtime unsupported decisions, each singular
role duplicate and invalid generation-hook signatures before source changes.
Run the exact focused command and preserve a FIX01 RED, then implement narrowly
and rerun focused GREEN, standalone C02, targeted authority/cardinality/hook
tests, scoped Ruff/format, AST purity, `git diff --check` and hash checks.

#### 5. Execution discipline and breakers

The coding subagent remains sole source/test writer; no artifact edit, stage,
commit or push. Do not reject existing trusted compatibility methods merely
because they accept store/lease/revision arguments, do not invoke lifecycle
hooks during validation, and do not add reflection-driven execution,
dependencies or files.

#### 6. Delivery boundary

The original C02 commit subject and stage budget remain unchanged. Commit is
forbidden until FIX01 GREEN, settled-diff `sol / max` review PASS and
independent `terra / medium` mechanical PASS.

#### 7. Required final report

Return FIX01 RED/GREEN counts, rejected and permitted authority names, hook
signature cases, role/request negatives, exact paths/hashes, Ruff/diff/purity,
process cleanup and residuals.

### 24.4 MR0-C02-FIX01 test-first execution evidence

The `gpt-5.6-sol / high` coding subagent added the four requested regression
groups before changing source. The exact focused command then exited `1` with
`19 failed, 58 passed`:

- one cross-runtime unsupported extension was admitted;
- three singular artifact-role duplicates were admitted;
- ten snake/camel class authority names and one instance authority name were
  admitted;
- four lifecycle-hook forms with a required positional or keyword-only
  argument were admitted.

After the narrow correction, the same command returned `77 passed` (`31`
generic runtime cases plus `46` C02 cases). The standalone C02 suite returned
`46 passed`; the new targeted matrix returned `22 passed, 24 deselected`.

Admission now enumerates public class-MRO and instance-namespace names without
`dir()`, tokenizes snake and camel identifiers, and rejects the exact
commit/HEAD/Accept/Reject/review authority tokens. Tests cover both spellings
of `commit_revision`, `advance_head`, `accept_draft`, `reject_task` and
`review_task`, while permitting `open_revision` and private `_store` /
`_lease`. Unsupported extension decisions require the request runtime to equal
the decision runtime. Profiles retain exactly one native model, allow at most
one semantic observation, selector mapping and provenance declaration, and
continue to permit multiple exchange and evidence declarations. Registry
admission binds the signatures of the two bound generation hooks with zero
arguments but never invokes either hook; required positional/keyword-only
forms reject, while optional/default and variadic forms admit.

At `2026-07-26T03:44:48Z`, the controller independently reproduced:

```text
PYTHONPATH=src .venv/bin/python -m pytest -q \
  tests/test_runtime_contracts.py tests/test_runtime_registry.py \
  tests/test_cad_runtime.py
77 passed in 1.11s

.venv/bin/ruff check \
  src/vibecad/interaction/cad_runtime.py tests/test_cad_runtime.py
PASS

.venv/bin/ruff format --check \
  src/vibecad/interaction/cad_runtime.py tests/test_cad_runtime.py
PASS

git diff --check
PASS
```

Settled hashes are:

```text
0e7855250664c57115ddeeb4b073ff6d32626629fa09830ecaba760329613fc3  src/vibecad/interaction/cad_runtime.py
f26df05d5d62b806d15a6bde58ef932d48d995bdf9ee9f333ab3ae80b465a73a  tests/test_cad_runtime.py
f1e9b6e50b2042c09dff60d024a6fbf53ee09f2507b6b66dfa0423de9ae776a5  src/vibecad/interaction/__init__.py
```

Only the two new implementation paths and this controller-owned artifact are
changed. No stage, commit or push has occurred. The initial architecture review
red remains preserved; the settled-diff `sol / max` re-review and independent
`terra / medium` mechanical gate remain mandatory.

| Entry ID | Decision / approval | Commit / push | Gate evidence | Residual | Snapshot | State |
|---|---|---|---|---|---|---|
| MR0-C02-E03 | D01; D03; D05; D06; A01; A02 | `not-created`; forbidden | FIX01 RED 19/58; focused GREEN 77; C02 46; targeted 22; controller tests/Ruff/diff/hash PASS | none | MRG1-S04 | corrected / re-review pending |

### 24.5 MR0-C02-E04 — settled-diff adversarial PASS

At `2026-07-26T03:46:35Z`, the independent `gpt-5.6-sol / max`
architecture/adversarial re-review returned PASS with Critical `0`, Major `0`,
Medium `0`, Minor `0`. It independently reproduced the exact focused suite as
`77 passed in 1.13s`, plus Ruff lint/format, diff, syntax, whitespace, EOF and
import-purity PASS.

Independent probes closed each preserved review finding:

- public names from the class MRO and instance namespace reject all requested
  snake/camel authority forms; `open_revision` and private `_store` /
  `_lease` remain compatible, and a hostile `__dir__` plus lifecycle bodies
  remain uncalled;
- cross-runtime unsupported extension construction rejects, while a
  same-runtime configured unsupported rule remains reachable;
- semantic observation, selector mapping and provenance cardinalities are
  singular, while exchange/evidence multiplicity remains valid;
- required positional and keyword-only generation hooks reject; optional and
  variadic forms bind zero arguments and no hook body executes at admission.

The reviewer also rechecked exact runtime/version routing, immutable admitted
descriptors, five decision branches, non-executable rejection before adapter
selection, selector authority, artifact qualification, bounded hostile
iteration, narrow Protocol authority and the subprocess timeout margin. No
new finding or waiver remains. The reviewed hashes equal Section 24.4.

| Entry ID | Decision / approval | Commit / push | Gate evidence | Residual | Snapshot | State |
|---|---|---|---|---|---|---|
| MR0-C02-E04 | D01; D03..D07; A01; A02 | `not-created`; forbidden | focused 77; Ruff/diff/purity PASS; independent review 0/0/0/0; four prior findings closed | none | MRG1-S04 | review PASS / mechanical gate pending |

### 24.6 MR0-C02-E05 — independent pre-stage mechanical PASS

At `2026-07-26T03:49:32Z`, the distinct `gpt-5.6-terra / medium`
mechanical-gate subagent returned PASS without waiver or state change:

```text
HEAD/upstream:       07c6d6cd0260dcce41711a4a92d47132460571db
allowed paths:       3 exact
staged paths:        0
out-of-allowlist:    0
focused:             77 passed in 1.37s
Ruff lint/format:    PASS / PASS
diff/whitespace/EOF: PASS
process leaks:       0
```

It independently matched all three settled hashes, the exact three internal
`vibecad` imports, zero source assertions, the exact four-member adapter
Protocol and the unchanged initializer. Artifact headings were consecutive
and unique from Sections 1 through 24 with Section 24 terminal; all required
RED, review-red, FIX01, GREEN and re-review evidence was present, and the
artifact made no positive C02 commit/push claim.

| Entry ID | Decision / approval | Commit / push | Gate evidence | Residual | Snapshot | State |
|---|---|---|---|---|---|---|
| MR0-C02-E05 | D01..D15; A01; A02 | `not-created`; exact staging next | pre-stage mechanical PASS; focused 77; hashes/imports/Protocol/ledger exact | none | MRG1-S04 | gated / ready to stage |

### 24.7 MR0-C02-E06 — post-stage mechanical PASS

At `2026-07-26T03:51:45Z`, the `gpt-5.6-terra / medium` subagent
rechecked the staged-only candidate and returned PASS:

```text
cached paths:        3 exact
unstaged/untracked:  0 / 0
cached diff check:   PASS
focused:             77 passed in 1.16s
Ruff lint/format:    PASS / PASS
process leaks:       0
```

The staged source/test blob hashes matched Section 24.4, the initializer
remained byte-identical, and the staged artifact retained consecutive unique
Sections 1 through 24 with all RED/GREEN/review evidence and no positive claim
that the C02 commit or push already existed. The artifact is restaged after
adding this evidence; a final cached-only integrity check remains before
commit creation.

| Entry ID | Decision / approval | Commit / push | Gate evidence | Residual | Snapshot | State |
|---|---|---|---|---|---|---|
| MR0-C02-E06 | D01..D15; A01; A02 | `not-created`; commit next | post-stage mechanical PASS; cached 3; unstaged/untracked 0/0; focused 77; hashes exact | none | MRG1-S04 | gated / ready to commit |

## 25. MR0-C02 Finalization and MR0-C03 Task Packet

### 25.1 MR0-C02 accepted commit

The exact three-path candidate was committed as:

```text
6c3581bab14434ba7c1301e033e973d59907cc4d
feat(cad): add backend-neutral CAD runtime port
```

The commit was pushed immediately to `codex/agent-stage3`. At
`2026-07-26T03:58:29Z`, local HEAD and upstream both resolved to the full hash
above and the worktree was clean. The commit created only
`cad_runtime.py` and `test_cad_runtime.py` and appended this artifact; the
package initializer remained byte-identical.

The accepted evidence chain preserves the genuine absent-module RED, the
design-correction RED, the initial adversarial review red, FIX01 test-first
RED/GREEN, independent `sol / max` `0/0/0/0`, independent
`terra / medium` pre/post-stage PASS and final cached-only integrity PASS.
No waiver or residual defect was carried into C03.

| Entry ID | Decision / approval | Commit / push | Gate evidence | Residual | Snapshot | State |
|---|---|---|---|---|---|---|
| MR0-C02-E07 | D01..D15; A01; A02 | `6c3581bab14434ba7c1301e033e973d59907cc4d`; pushed | RED/design/review-red/FIX01 preserved; GREEN 77; review 0/0/0/0; mechanical PASS; local/upstream equal | none | MRG1-S05 | accepted / closed |

### 25.2 MR0-C03 read-only architecture audit

The `gpt-5.6-sol / max` audit returned PASS without editing, staging,
committing or pushing. It traced the current default path from
`AgentApplication.open()` through `_cad_execution_port_under_gate()` to
`project._default_cad_port_factory()`, then through `build_project_runtime()`.
The application already constructs the default Worker lazily once, verifies
the nominal `CadExecutionPort`, caches that same object and uses it for
revision sessions, snapshots and Task execution. Cancellation, generation-loss
observation and application close already call the Worker's existing
generation hooks directly.

The audit found one structural gap only:
`WorkerCadExecutionPort` already has the required locked `generation_lost`
property and zero-argument `terminate_generation()` / `close_generation()`
hooks, but has no immutable `runtime_descriptor`. Its constructor, twelve
private slots and lifecycle implementation need no change.

The approved narrow design is:

- construct one module-level frozen runtime descriptor from the authoritative
  `runtime.spec.FREECAD_VERSION`, yielding exact identity
  `cad/freecad@1.1.0`;
- declare only `authoring.execute_program@1`, execution profile `headless`,
  native artifact `native_model` /
  `application/vnd.freecad.fcstd` and exchange artifact
  `exchange_model` / `model/step`;
- return that singleton from a read-only Worker property and retain structural
  Protocol compatibility rather than adding inheritance;
- inside the lazy default factory, construct one nominal Worker, register that
  exact object, route `CAD_EXECUTE_PROGRAM_V1` through
  `CadDomainService(CadRuntimeRouter(...))`, explicitly fail closed unless
  `selected is worker`, and return the original Worker unchanged;
- on the impossible mismatch, close only the factory-created Worker before
  raising a fixed `TypeError`.

Imports in the default factory stay local. No Worker wrapper, second
generation state machine, generic `RuntimeControlPort`, public operation,
tool, Task/Revision authority or durable artifact schema is introduced.

Read-only baselines before C03 source/test changes were:

```text
C02 plus nominal/lazy/lifecycle nodes: 50 passed in 2.34s
first approved C03 compatibility set:  375 passed, 23 deselected in 28.42s
second approved C03 compatibility set: 379 passed, 1 deselected in 48.04s
```

### 25.3 MR0-C03 seven-section implementation packet

#### 1. Authorization

MRG1-A01 and A02 authorize C03 under D01–D15. Routine test-first coding is
routed to `gpt-5.6-sol / high`; settled architecture/adversarial review stays
`gpt-5.6-sol / max`; mechanical and process gates stay
`gpt-5.6-terra / medium`. This packet connects the delivered FreeCAD Worker
to the internal CAD runtime boundary without expanding product support.

#### 2. Workspace anchor and exact write scope

Start from pushed commit
`6c3581bab14434ba7c1301e033e973d59907cc4d`. The coding subagent may write
only:

```text
src/vibecad/application/project.py
src/vibecad/execution/worker_port.py
tests/test_agent_application.py
tests/test_cad_execution_port.py
```

`tests/test_freecad_worker.py` remains in the approved commit allowlist but is
gate-only unless a genuine compatibility gap first requires a controller
decision. This artifact is controller-owned. All other paths are forbidden.

#### 3. Required implementation and invariants

In `worker_port.py`, add only the imports and module-level immutable values
needed for the exact FreeCAD descriptor plus a read-only
`runtime_descriptor` property. Derive `1.1.0` from
`runtime.spec.FREECAD_VERSION`; do not duplicate the version literal. The
descriptor declares exactly the internal aggregate capability, headless
profile and two artifact declarations recorded in Section 25.2. Repeated
property access must return the same descriptor object.

Do not change `WorkerCadExecutionPort` inheritance, constructor, `__slots__`,
existing properties, worker startup, locks, generation state, lifecycle hooks,
error translation or FCStd/STEP behavior.

In `project._default_cad_port_factory()`, retain lazy local imports. Construct
one Worker, register that exact instance in `CadRuntimeAdapterRegistry`, route
the descriptor identity and `CAD_EXECUTE_PROGRAM_V1` through
`CadDomainService` and `CadRuntimeRouter`, explicitly require object identity
with the Worker, then return that Worker. Normal composition must not start a
FreeCAD process. A mismatched selection closes the factory-owned Worker and
raises; it never returns or closes the foreign selection.

#### 4. Test-first steps and gates

Before source edits, add three tests:

1. the Worker is one lazy structural `CadRuntimeAdapter` with the exact
   singleton descriptor, capability/profile/artifact declarations and zero
   admission hook calls;
2. the default factory routes the exact internal capability and makes the
   registered, selected and returned object the same nominal Worker without
   starting FreeCAD;
3. a non-identical routed adapter fails closed and cleans up only the created
   Worker.

Run and preserve the genuine three-node RED:

```text
PYTHONPATH=src .venv/bin/python -m pytest -q \
  tests/test_cad_execution_port.py::test_worker_port_is_one_lazy_immutable_freecad_runtime_adapter \
  tests/test_agent_application.py::test_default_cad_port_factory_routes_and_returns_exact_nominal_worker \
  tests/test_agent_application.py::test_default_cad_port_factory_rejects_nonidentical_routed_adapter
```

Then implement the two source changes and rerun those nodes to GREEN. Run the
two approved C02 dependency guards, both prewritten C03 compatibility commands
from Section 13.5, scoped Ruff/format, `git diff --check`, exact allowlist,
module-load/lazy-start and process-leak checks. Run the managed FreeCAD smoke
and both real Task Kernel gates exactly as Section 13.5 specifies; the Task
Kernel command may not use `VIBECAD_FREECAD_ENV`.

#### 5. Execution discipline and breakers

One coding subagent owns the four implementation/test paths and may not edit
the artifact, stage, commit or push. Stop on a top-level project import that
eagerly imports Worker/runtime selection, a per-access descriptor rebuild,
wrong MIME/version, use of optimized-away `assert`, a second Worker or wrapper,
an edit to Worker slots/signatures/lifecycle, routing of individual lifecycle
calls, a public capability/tool change, a revision/store/schema edit, an
allowlist escape, a test-count decrease, new deselection/warning or leaked
process.

#### 6. Delivery boundary

The intended commit is exactly:

```text
refactor(freecad): route worker through CAD runtime adapter
```

It contains only the accepted implementation/test paths and this artifact.
Exact named staging, commit and immediate push remain controller-only and may
occur only after focused/compatibility/real gates, `sol / max` review and
`terra / medium` mechanical PASS.

#### 7. Required final report

Return the three RED causes/count, targeted and compatibility GREEN counts,
exact descriptor values and singleton evidence, registered/selected/returned
object identity, lazy-start and cleanup counters, confirmation that Worker
constructor/slots/hooks stayed unchanged, public 28-tool and six-operation
evidence, Task/Revision/Accept/Reject and FCStd/STEP compatibility, real gate
results, paths/hashes, Ruff/diff/process evidence, review severities,
residuals and any breaker.

| Entry ID | Decision / approval | Commit / push | Gate evidence | Residual | Snapshot | State |
|---|---|---|---|---|---|---|
| MR0-C03-E01 | D01..D15; A01; A02 | `not-created`; forbidden | design audit PASS; pre-change baselines 50, 375/23, 379/1 | MRG1-RES-02 until C04 | MRG1-S05 | packet issued / test-first RED next |

### 25.4 MR0-C03 genuine test-first RED

The `gpt-5.6-sol / high` coding subagent created and syntax-checked the three
approved tests before changing either source file. The exact three-node
command exited `1` with `3 failed in 0.95s`:

1. the Worker had no `runtime_descriptor` and raised `AttributeError`;
2. the default factory returned its Worker without registering or routing it,
   so the selection record remained empty;
3. the foreign-selection branch did not exist, so the fixed `TypeError` was
   not raised.

These were contract REDs, not syntax, setup or dependency failures. No Worker
factory or FreeCAD process started. The implementation subagent may now make
only the two approved narrow source edits; no stage, commit or push is
permitted.

| Entry ID | Decision / approval | Commit / push | Gate evidence | Residual | Snapshot | State |
|---|---|---|---|---|---|---|
| MR0-C03-E02 | D03; D05; D06; D08; A01; A02 | `not-created`; forbidden | three-node RED 3 failed; syntax PASS; zero Worker starts | MRG1-RES-02 until C04 | MRG1-S05 | RED preserved / implementation active |

### 25.5 MR0-C03 focused GREEN and compatibility evidence

The coding subagent made only the four authorized implementation/test changes.
The resulting Worker descriptor is a private module-level singleton with:

```text
identity:             cad/freecad@1.1.0
capabilities:         authoring.execute_program@1
execution profiles:  headless
decisions:            none (implicit native planning)
native artifact:      native_model / application/vnd.freecad.fcstd / v1
exchange artifact:    exchange_model / model/step / v1
```

The version is derived from `runtime.spec.FREECAD_VERSION`. Repeated property
access returns the same descriptor object. The Worker's nominal inheritance,
constructor, twelve slots, locks, generation state and lifecycle hooks are
unchanged.

The default factory retains local lazy imports and constructs one Worker. The
tests explicitly prove the registry's admitted object, the service-selected
object and the returned nominal `WorkerCadExecutionPort` are identical, and
that the routed identity/capability are exact. Normal discovery/start counters
remain zero. A foreign selection closes only the one factory-created Worker,
does not close the foreign object and raises the fixed mismatch `TypeError`.

Evidence at `2026-07-26T04:17:54Z`:

```text
three-node GREEN:          3 passed in 0.46s
C02 dependency guards:    2 passed in 0.12s
compatibility command 1:  376 passed, 23 deselected in 21.64s
compatibility command 2:  381 passed, 1 deselected in 46.13s
Ruff lint/format:          PASS / PASS
git diff --check:          PASS
pytest/FreeCAD leak scan:  0
```

The compatibility counts increased by exactly one and two tests respectively
from the read-only baseline; deselections did not increase. A second
independent `gpt-5.6-sol / max` design validation also returned PASS and
reproduced the Worker authority surface, zero-start routing and exact
descriptor values without editing the workspace.

Settled implementation/test hashes before real-runtime gates are:

```text
c686f91dd8189bae92f505ceeb586dc4eec5cb60159c4612dcc2387462d8f5e6  src/vibecad/application/project.py
2426dc7b3d41473e7aa5aeabadef31e0fe9a03068f4aa61d946564b8f445546b  src/vibecad/execution/worker_port.py
70492dfcc1c26df2828edda3e043f49282f932e9861f2ced9ff6369033cbafe2  tests/test_agent_application.py
72d3e3ee56eb8360203af128221603274de445bde4dde2a743672a139f9111a9  tests/test_cad_execution_port.py
```

Only those four paths and this controller artifact are modified; no path is
staged or untracked. Real managed-runtime gates, settled-diff adversarial
review and mechanical staging checks remain mandatory.

| Entry ID | Decision / approval | Commit / push | Gate evidence | Residual | Snapshot | State |
|---|---|---|---|---|---|---|
| MR0-C03-E03 | D03; D05; D06; D08; A01; A02 | `not-created`; forbidden | GREEN 3; guards 2; compatibility 376/23 and 381/1; Ruff/diff/leak PASS; two design reviews PASS | MRG1-RES-02 until C04 | MRG1-S05 | implemented / real gates pending |

### 25.6 MR0-C03 real managed-runtime PASS

At `2026-07-26T04:20:37Z`, the independent
`gpt-5.6-terra / medium` gate verified the current managed FreeCAD
`1.1.0` generation, exact receipt and readiness, then returned PASS:

```text
managed Worker load/modify/checkpoint/export: 1 passed in 1.97s
real Task Kernel gates:                       2 passed in 17.39s
skip / deselect / warning:                    0 / 0 / 0
pytest/FreeCAD/Worker/daemon leaks:            0
```

The managed Worker command used the exact selected Python:

```text
/Users/wangtao/Library/Application Support/VibeCAD/runtime/mamba/envs/vibecad/bin/python
```

The Task Kernel command explicitly removed `VIBECAD_FREECAD_ENV`, enabled
`VIBECAD_RUN_INTEGRATION=1` and verified that the optional managed-Python
value matched the selected generation. Both real tests executed; none skipped
or deselected. The runtime receipt remained current and `runtime_ready=True`
afterward. The exact five changed paths and all four implementation/test
hashes remained unchanged.

| Entry ID | Decision / approval | Commit / push | Gate evidence | Residual | Snapshot | State |
|---|---|---|---|---|---|---|
| MR0-C03-E04 | D01; D03; D08; A01; A02 | `not-created`; forbidden | real Worker 1; real Task Kernel 2; override absent; skips/warnings/leaks 0 | MRG1-RES-02 until C04 | MRG1-S05 | real gates PASS / review pending |

### 25.7 MR0-C03 settled-diff adversarial PASS

At `2026-07-26T04:31:11Z`, the independent `gpt-5.6-sol / max`
architecture/adversarial review returned PASS with Critical `0`, Major `0`,
Medium `0`, Minor `0`. The four implementation/test hashes remained equal to
Section 25.5 from the beginning through the end of review.

The review independently established:

- private immutable descriptor singleton, exact derived version/capability/
  profile/artifact metadata, empty decisions and unchanged `__all__`;
- nominal `CadExecutionPort` plus structural four-member
  `CadRuntimeAdapter`, with constructor, exact twelve slots, locks,
  lifecycle properties and hook bodies unchanged from the anchor;
- authority admission permits trusted `open_revision` and private store state
  without granting commit/HEAD/review/Task authority or invoking startup/hooks;
- one default Worker, exact registry/router/service path and explicit object
  identity in both normal and optimized mode;
- reachable mismatch cleanup closes only the unpublished owned Worker and
  cannot mask the fixed `TypeError`; the foreign selection remains untouched;
- immutable admission snapshots prevent descriptor drift/TOCTOU;
- no eager Worker/FreeCAD service load, public surface remains 28 tools and
  six semantic operations, and no Task/Revision/schema/artifact-payload code
  changed.

Independent reruns returned:

```text
targeted / guards:       3 / 2 passed
compatibility command 1: 376 passed, 23 deselected
compatibility command 2: 381 passed, 1 deselected
lazy module-load:         1 passed
Ruff/format/diff/imports: PASS
optimized-mode probes:    PASS
process leaks:            0
```

No waiver, residual finding or corrective packet is required.

| Entry ID | Decision / approval | Commit / push | Gate evidence | Residual | Snapshot | State |
|---|---|---|---|---|---|---|
| MR0-C03-E05 | D01..D15; A01; A02 | `not-created`; forbidden | real gates PASS; review 0/0/0/0; 28 tools; 6 operations; optimized/lazy/AST/TOCTOU PASS | MRG1-RES-02 until C04 | MRG1-S05 | review PASS / mechanical gate pending |

### 25.8 MR0-C03 independent pre-stage mechanical PASS

At `2026-07-26T04:36:32Z`, the distinct
`gpt-5.6-terra / medium` subagent returned PASS without changing state:

```text
HEAD/upstream:         6c3581bab14434ba7c1301e033e973d59907cc4d
changed paths:         5 exact
staged/untracked:      0 / 0
targeted / guards:     3 / 2 passed
Ruff/format/diff:      PASS
whitespace/EOF/leaks:  0 / 0 / 0
```

It independently matched all four hashes, exact twelve Worker slots,
unchanged lifecycle AST bodies, private descriptor/`__all__`, lazy local
factory imports, one Worker construction, explicit object-identity check,
zero forbidden eager modules, 28 public tools and six direct semantic
operations. Sections 1 through 25 were consecutive and unique with Section 25
terminal; all C03 RED/GREEN/compatibility/real/review evidence was present and
there was no positive C03 commit or push claim.

| Entry ID | Decision / approval | Commit / push | Gate evidence | Residual | Snapshot | State |
|---|---|---|---|---|---|---|
| MR0-C03-E06 | D01..D15; A01; A02 | `not-created`; exact staging next | pre-stage mechanical PASS; five paths/four hashes exact; 28/6; AST/lazy/ledger PASS | MRG1-RES-02 until C04 | MRG1-S05 | gated / ready to stage |

### 25.9 MR0-C03 post-stage mechanical PASS

At `2026-07-26T04:37:52Z`, the `gpt-5.6-terra / medium` subagent
returned PASS for the staged-only candidate:

```text
cached paths:        5 exact
unstaged/untracked:  0 / 0
cached diff check:   PASS
targeted / guards:   3 / 2 passed
Ruff lint/format:    PASS / PASS
process leaks:       0
```

The staged blobs and worktree matched all four settled hashes. The staged
artifact retained consecutive unique Sections 1 through 25 with Section 25
terminal, the pre-stage evidence and no claim that C03 had already been
committed or pushed. This artifact is restaged after adding the post-stage
record; one final cached-only integrity check remains before commit creation.

| Entry ID | Decision / approval | Commit / push | Gate evidence | Residual | Snapshot | State |
|---|---|---|---|---|---|---|
| MR0-C03-E07 | D01..D15; A01; A02 | `not-created`; commit next | post-stage mechanical PASS; cached 5; unstaged/untracked 0/0; targeted/guards/Ruff/hash exact | MRG1-RES-02 until C04 | MRG1-S05 | gated / ready to commit |

## 26. MR0-C03 Finalization and MR0-C04 Task Packet

### 26.1 MR0-C03 accepted commit

The exact five-path candidate was committed as:

```text
71a25b583363fcbd3c4f8cf56c3cde594194e648
refactor(freecad): route worker through CAD runtime adapter
```

The commit was pushed immediately to `codex/agent-stage3`. At
`2026-07-26T04:39:28Z`, local HEAD and upstream both resolved to that full
hash and the worktree was clean.

The accepted evidence preserves the genuine three-failure RED, targeted
GREEN, both compatibility suites, current managed Worker and Task Kernel real
gates, two design reviews, settled-diff `0/0/0/0`, independent pre/post-stage
mechanical PASS and final cached-only integrity PASS. Public tools remain 28,
semantic operations remain six, and the Worker constructor, twelve slots and
lifecycle hooks remain unchanged.

| Entry ID | Decision / approval | Commit / push | Gate evidence | Residual | Snapshot | State |
|---|---|---|---|---|---|---|
| MR0-C03-E08 | D01..D15; A01; A02 | `71a25b583363fcbd3c4f8cf56c3cde594194e648`; pushed | RED/GREEN preserved; compatibility 376/23 and 381/1; real 1+2; review 0/0/0/0; mechanical PASS; local/upstream equal | MRG1-RES-02 until C04 | MRG1-S06 | accepted / closed |

### 26.2 MR0-C04 read-only architecture audit

Two `gpt-5.6-sol / max` reviews converged on PASS with one explicit contract
caveat. C01's `RuntimeControlPort` intentionally exposes only
`start/get_status/cancel/reconcile/health`; it has no result-retrieval hook.
C04 therefore may not invent a second retrieval interface or actively run
arbitrary provider code from production conformance modules.

The approved interpretation of D11 is:

- deterministic fake runtimes in tests really execute start, status, cancel,
  reconcile and health and produce immutable observations/results;
- the reusable production kit is a pure transcript/value evaluator;
- the five control methods are inspected statically on a supplied ordinary
  class without instantiation or invocation;
- the fake's concrete successful `RuntimeResult` is supplied beside the
  transcript and is not misrepresented as a value retrieved through
  `RuntimeControlPort`.

This stays within the approved C04 scope. If a future stage requires result
retrieval through the generic port itself, it must version C01 separately.

The CAD evaluator consumes one already-admitted
`CadRuntimeAdapterRegistry` snapshot. It may route through that snapshot but
must not reread provider descriptor properties or invoke lifecycle hooks.
Authority-negative admission is a separate narrow evaluator that delegates
once to the existing registry and records only stable codes, never exception
messages or representations.

#### Recommended immutable APIs

`vibecad.runtime.conformance`:

```text
ConformanceFinding(code, case_id, subject)
ConformanceReport(findings) -> conforms property
RuntimeSuccessTranscript(invocation, start_status, final_status, result)
RuntimeCancellationTranscript(invocation, start_status, cancel_status,
                              reconciled_status)
RuntimeConformanceCase(case_id, descriptor, control_class, success,
                       cancellation, health)
evaluate_runtime_conformance(cases)
```

`vibecad.interaction.cad_conformance`:

```text
CadRuntimeAdmissionCase(case_id, adapter)
CadRuntimeConformanceCase(case_id, registry, identity, executable_request,
                          unsupported_request, artifacts, selector)
evaluate_cad_runtime_admission(cases)
evaluate_cad_runtime_conformance(cases)
```

All value types are frozen/slots and all report data is bounded. Case
collections are bounded to `32`, findings to `128`, case IDs to `64` ASCII
contract characters, and CAD artifacts to `32`. Findings contain only a fixed
code, validated case ID (or a fixed ordinal fallback) and fixed subject; no
free-form provider text, path, class name, annotation or exception rendering
is admitted. Results are sorted by `(case_id, code, subject)`. Duplicate case
IDs produce deterministic findings and no member of that duplicate group is
evaluated. Endless/hostile collections fail within the same bounds.

Generic stable code families cover case bound/identity, duplicate IDs,
missing/invalid control method signatures, forbidden authority, identity or
capability mismatch, invalid success/cancellation transitions, result
correlation/provenance and health identity. CAD codes cover admission
authority/failure, registry identity, rejected executable request, accepted
unsupported request, undeclared capability, artifact runtime/kind/media,
required semantic selector envelope and report bound.

Static control inspection uses class namespaces/MRO and signatures only. It
requires synchronous instance forms compatible with:

```text
start(self, invocation)
get_status(self, invocation_id)
cancel(self, invocation_id, *, reason)
reconcile(self, invocation_id)
health(self, identity)
```

It does not inspect annotations, instantiate the class or call a method.
Commit/HEAD/Accept/Reject/review authority tokens fail. Private state and
non-authority result helpers remain outside the five-method contract.

The generic transcript checks exact descriptor identity/capability/profile,
stable invocation correlation, accepted start state, terminal successful
status/result, exact result runtime/provenance/artifacts, a distinct
cancellation invocation ending and reconciling as cancelled, and exact health
identity. CAD checks use only the admitted descriptor snapshot, require an
executable exact declared request, require an undeclared request to remain
non-executable without fallback, validate concrete artifacts against the
runtime-qualified profile and require a `CadSelectorEnvelope`; a bare
`NativeLocator` fails closed.

The pre-test exact C04 command returned pytest usage exit `4` because the first
new test path did not yet exist. That is baseline evidence, not a RED. The
valid test-first RED occurs only after all three test modules exist, syntax
check, contain substantive assertions and fail collection solely because the
two production conformance modules are deliberately absent. Existing
C01/C02/C03 focused dependencies were green (`77 passed`) during audit.

### 26.3 MR0-C04 seven-section implementation packet

#### 1. Authorization

MRG1-A01/A02 and D01, D03–D07, D10 and D11 authorize C04. Routine coding uses
`gpt-5.6-sol / high`; architecture/adversarial review uses
`gpt-5.6-sol / max`; mechanical gates use
`gpt-5.6-terra / medium`. C04 proves portability contracts with deterministic
fakes; it does not claim a delivered second runtime.

#### 2. Workspace anchor and exact write scope

Start from pushed commit
`71a25b583363fcbd3c4f8cf56c3cde594194e648`. The coding subagent may create
only:

```text
src/vibecad/runtime/conformance.py
src/vibecad/interaction/cad_conformance.py
tests/test_runtime_conformance.py
tests/test_cad_runtime_conformance.py
tests/test_runtime_purity.py
```

This artifact is controller-owned. Package initializers, C01/C02/C03 source,
application/Worker/Task/store/revision/public modules and all other paths must
remain unchanged.

#### 3. Required implementation and invariants

Implement the immutable APIs, transcript semantics, static control-class
inspection, admitted CAD snapshot evaluation, stable codes, deterministic
ordering and bounds from Section 26.2. Production modules may not depend on
pytest and may not execute a provider lifecycle method. CAD admission may call
only the existing `CadRuntimeAdapterRegistry` constructor once per bounded
case; CAD conformance must use the supplied registry snapshot without provider
descriptor rereads.

`runtime.conformance` may import only stdlib and generic runtime
contracts/registry. It must not import interaction/CAD/FreeCAD/Worker,
application, workflow, Task, store, revision, Qt, FEA or reconstruction.
`cad_conformance` may import stdlib, the generic conformance/contracts layer
and `cad_runtime`; it may not import FreeCAD/Worker/application/workflow/
store/revision/public tools. Both modules contain no `assert`.

#### 4. Test-first steps and gates

Create all three test files first and syntax-check them. They must already
contain substantive lifecycle, authority, artifact, selector, unsupported,
bound, deterministic and purity assertions. Run the exact Section 13.5 C04
command before source creation and preserve exit `2` whose collection errors
are only the deliberately absent conformance modules; existing tests must
collect normally.

Then implement the two source modules and rerun the same exact command to
GREEN:

```text
PYTHONPATH=src .venv/bin/python -m pytest -q \
  tests/test_runtime_contracts.py tests/test_runtime_registry.py \
  tests/test_cad_runtime.py tests/test_runtime_conformance.py \
  tests/test_cad_runtime_conformance.py tests/test_runtime_purity.py
```

Tests must include:

- a deterministic fake control port actually exercising success,
  cancellation and reconciliation before transcript evaluation;
- exact lifecycle/result/artifact/provenance/health correlations;
- static missing/wrong/async control signatures and commit/HEAD-like
  authority negatives with zero constructor/method calls;
- two fake CAD identities and exact admitted snapshot routing;
- good adapter admission plus commit/HEAD authority rejection;
- runtime/artifact kind/media mismatch, bare native locator, and undeclared
  capability rejection before hook calls;
- duplicate IDs, reversed input order, hostile/endless cases and finding
  bounds;
- AST import purity, no pytest/assert, unchanged package initializer hashes,
  optimized-mode behavior and zero forbidden module load.

Run scoped Ruff/format, `git diff --check`, exact allowlist, hashes and process
cleanup. Settled code requires independent `sol / max` `0/0/0/0` and
`terra / medium` pre/post-stage PASS.

#### 5. Execution discipline and breakers

One coding subagent owns the five implementation/test paths and may not edit
the artifact, stage, commit or push. Stop if tests require a C01/C02/C03 edit,
an active result-retrieval hook, production provider invocation, registry/
router duplication, provider descriptor reread, pytest in source, unstable
messages/reprs/paths, unbounded input, package initializer edit, public
support claim, out-of-allowlist path, existing-count decrease, new warning/
deselection or leaked process.

#### 6. Delivery boundary

The intended commit is exactly:

```text
test(runtime): enforce adapter conformance
```

It may contain only the five new files and this artifact. Exact staging,
commit and immediate push are controller-only after RED/GREEN, purity/bound
gates, adversarial review and mechanical PASS.

#### 7. Required final report

Return the genuine collection RED cause/count, exact GREEN count, fake
lifecycle call/transition/result evidence, all stable negative codes, bounds
and deterministic ordering, zero-constructor/hook/provider-read counters,
CAD snapshot/artifact/selector/unsupported evidence, purity/import/init hashes,
optimized/hostile/process results, exact paths/hashes, review severities,
residuals and breakers.

| Entry ID | Decision / approval | Commit / push | Gate evidence | Residual | Snapshot | State |
|---|---|---|---|---|---|---|
| MR0-C04-E01 | D01; D03..D07; D10; D11; A01; A02 | `not-created`; forbidden | two design audits PASS; pre-test exit 4 not RED; dependency baseline 77 | MRG1-RES-02 | MRG1-S06 | packet issued / test-first RED next |

### 26.4 MR0-C04 genuine test-first RED

The `gpt-5.6-sol / high` coding subagent created three substantive test
modules before either production module. AST parsing and isolated bytecode
compilation passed. The exact C04 command then exited `2` with
`2 errors in 0.48s`.

The only collection causes were the deliberately absent
`vibecad.runtime.conformance` and
`vibecad.interaction.cad_conformance` modules. Existing C01 contracts/
registry and C02 CAD tests had no collection, syntax, setup or dependency
failure. This is the accepted genuine RED; the earlier missing-test-path exit
`4` remains explicitly excluded.

The two production evaluators may now be created within the exact packet.
No stage, commit or push is permitted.

| Entry ID | Decision / approval | Commit / push | Gate evidence | Residual | Snapshot | State |
|---|---|---|---|---|---|---|
| MR0-C04-E02 | D03; D05..D07; D10; D11; A01; A02 | `not-created`; forbidden | three tests substantive; syntax PASS; exact RED exit 2 with two deliberate missing-module errors | MRG1-RES-02 | MRG1-S06 | RED preserved / implementation active |

### 26.5 MR0-C04 implementation GREEN and settled review candidate

The first exact post-source run produced `91 passed, 2 failed`. Both failures
were isolated to new-test harness assumptions rather than product behavior:
the artifact-bound fixture accidentally declared its nominally unsupported
capability as supported, and the import-purity subprocess compared against an
empty module set instead of subtracting the already-loaded interaction
package-initializer baseline. The coding subagent changed only those two
fixtures. It did not remove a product assertion, relax a finding code, increase
a bound or change a production module to satisfy them. The corrected exact run
was `93 passed`.

The controller then required the unsupported CAD path to prove the real
supplied-registry routing boundary rather than only inspect a plan. The
evaluator now constructs `CadRuntimeRouter` over the admitted registry
snapshot, calls `adapter_for` for the non-executable decision and accepts only
the exact `NonExecutableCadDecisionError` carrying that same decision.
Invalid, accidentally accepted, unexpectedly returned and wrong-error paths
have distinct stable codes. The observer test proves one `adapter_for` call,
one exact error, zero lifecycle hooks and no provider descriptor/generation
reread. That strengthening added one test; the settled exact suite is
`94 passed` (coding subagent `1.69s`, controller `1.64s`).

The deterministic generic fake constructs the evidence by actually calling:

```text
start(success) -> get_status(SUCCEEDED) ->
start(cancel) -> cancel(CANCELLED) ->
reconcile(CANCELLED) -> health
```

That is six lifecycle calls plus one test-only concrete-result helper. The
production evaluator does not add a constructor, helper or lifecycle call.
Authority-negative classes remain at zero constructors and zero method calls.
Static inspection uses ordinary class MRO namespaces and
`inspect.signature(..., follow_wrapped=False, eval_str=False)`.

Admission constructs the existing registry once per bounded case. For the
good adapter, descriptor and generation are each read once during admission;
the authority-negative adapter remains at zero reads. Evaluation over two
admitted identities leaves both counters at one and leaves terminate/close
hooks at zero. Findings contain only fixed code, bounded case ID/fallback and
fixed subject, sort by `(case_id, code, subject)`, and do not include provider
text, exception rendering, paths or class/annotation strings.

The settled hard bounds are `32` cases, `128` findings, `64` ASCII contract
characters per case ID and `32` CAD artifacts. Duplicate groups are not
evaluated. Hostile and endless iterables fail closed within their corresponding
look-ahead bound. Optimized-mode behavior, AST import purity, absence of
pytest/`assert` in production, and zero incremental forbidden module loads
passed. Package initializer SHA-256 values remain:

```text
runtime/__init__.py
217184fec30d06cbe7f79f0c54589462f2ef1f23afb4ec75c36d37e02b86dee1
interaction/__init__.py
f1e9b6e50b2042c09dff60d024a6fbf53ee09f2507b6b66dfa0423de9ae776a5
```

Controller-scoped Ruff and format checks passed (`5 files already formatted`);
`git diff --check` passed. The settled implementation/test SHA-256 values are:

```text
src/vibecad/runtime/conformance.py
7e4867d5c253395355144007c7cc97b70e92b885bc587b5b93d5d2982343520e
src/vibecad/interaction/cad_conformance.py
0a9df3e6aee7d04624b6bec68143525e635a7d01a559939ecd9e68ce63aae65d
tests/test_runtime_conformance.py
edb71c97bdd43cfb6e1ed518637e2453b4dc057aea986915b6733bad24e26c82
tests/test_cad_runtime_conformance.py
e0b4db3aada809310fd00f5005db222e242416b1ef3d9822ef4645f7a365d815
tests/test_runtime_purity.py
04a118ffeae020da3de2f60959419d7f0123af2c718ed7bd0b7c0fcf836410f4
```

HEAD and upstream remain equal at
`71a25b583363fcbd3c4f8cf56c3cde594194e648`. The worktree contains only this
controller-owned artifact and the five authorized C04 paths. Nothing is
staged, committed or pushed. The settled candidate now requires independent
`gpt-5.6-sol / max` adversarial `0/0/0/0`, followed by delegated
`gpt-5.6-terra / medium` pre/post-stage mechanical gates.

| Entry ID | Decision / approval | Commit / push | Gate evidence | Residual | Snapshot | State |
|---|---|---|---|---|---|---|
| MR0-C04-E03 | D03; D05..D07; D10; D11; A01; A02 | `not-created`; forbidden | genuine RED preserved; first GREEN 91/2 harness red; corrected 93; strengthened exact 94; Ruff/format/diff/purity/bounds PASS | MRG1-RES-02 | MRG1-S06 | settled candidate / adversarial review active |

### 26.6 MR0-C04 review RED — atomic case-limit correction

Controller inspection found that `_prepare_cases` read the required 33rd
look-ahead item and emitted `case_limit_exceeded`, but still returned the first
32 unique cases for semantic evaluation. A direct CAD admission probe over 33
unique cases therefore produced the single stable limit code while invoking
the registry constructor 32 times.

An independent `gpt-5.6-sol / max` architecture adjudication classified this
as one high-severity/P1 merge blocker. The operations remained numerically
bounded, so this is not an unbounded/P0 defect, but an over-limit batch is
invalid as a whole. Evaluating its prefix is partial success rather than
fail-closed behavior and crosses the CAD admission/provider-read boundary for
an invalid batch.

The corrective scope is deliberately narrow:

- buffer at most 33 raw case references without inspecting case IDs,
  descriptors, registries or providers;
- on the 33rd item, return no prepared cases and exactly the stable
  generic/CAD case-limit finding;
- preserve raw-item counting so repeated IDs cannot bypass the limit;
- prove that an overflow batch reads exactly 33 items and never requests a
  34th;
- prove that generic prefix errors are not evaluated after overflow and CAD
  admission performs zero registry constructions/provider reads.

No public API or bound changes are authorized. The prior `94 passed` result is
preserved as pre-correction evidence, not accepted as the final GREEN. Nothing
is staged, committed or pushed.

| Entry ID | Decision / approval | Commit / push | Gate evidence | Residual | Snapshot | State |
|---|---|---|---|---|---|---|
| MR0-C04-E04 | D03; D05..D07; D10; D11; A01; A02 | `not-created`; forbidden | sol/max review 0 blocker / 0 critical / 1 major-high / 0 minor; controller probe: 33 cases caused 32 admissions | MRG1-RES-02 | MRG1-S06 | review RED / atomic-limit FIX required |

### 26.7 MR0-C04 atomic case-limit FIX GREEN

The `gpt-5.6-sol / high` coding subagent first added the two corrective
regressions and syntax-checked both test modules. The targeted run then
produced a genuine review RED: `2 failed in 0.22s`. The generic probe consumed
exactly 33 raw items but continued evaluating 32 poisoned prefix cases,
filling the bounded report with the limit code plus 127 semantic findings.
The CAD probe returned a superficially clean single limit code but constructed
the registry 32 times and consequently entered provider metadata reads. Neither
probe requested a 34th item.

The sole production correction changes the overflow return in
`_prepare_cases`. On obtaining the 33rd raw item it now immediately returns no
prepared cases and the single prefix-aware case-limit finding. It does not
inspect any buffered item's case ID, descriptor, registry or provider.
Validation, duplicate grouping, sorting and evaluation for collections of at
most 32 items remain unchanged.

The targeted GREEN was `2 passed`: the generic path consumes exactly 33 items
and emits exactly one limit finding with zero prefix semantics; CAD admission
performs zero registry constructions, zero descriptor/generation reads and
zero terminate/close calls. The exact C04 suite increased from 94 to
`96 passed` (coding subagent `1.67s`, controller `1.68s`). Controller-scoped
Ruff, five-file format check and `git diff --check` passed.

The new settled SHA-256 values are:

```text
src/vibecad/runtime/conformance.py
304ae29f71b7b512cdce06bd68f81090a37feeb4df27df7a586e673a244b08c3
src/vibecad/interaction/cad_conformance.py
0a9df3e6aee7d04624b6bec68143525e635a7d01a559939ecd9e68ce63aae65d
tests/test_runtime_conformance.py
c451554da1960e25b012b3935d779f68f24a003df748e7895bc82bdaf1a4fcc1
tests/test_cad_runtime_conformance.py
aba53b4b42caab674cf20d601dc28efde75757c5596d16fbbceb8d390c25d5a7
tests/test_runtime_purity.py
04a118ffeae020da3de2f60959419d7f0123af2c718ed7bd0b7c0fcf836410f4
```

Package initializer hashes remain unchanged. The worktree still contains only
the controller-owned artifact and five authorized C04 paths; nothing is
staged, committed or pushed. A fresh settled-diff `gpt-5.6-sol / max`
`0/0/0/0` is required before mechanical gating.

| Entry ID | Decision / approval | Commit / push | Gate evidence | Residual | Snapshot | State |
|---|---|---|---|---|---|---|
| MR0-C04-E05 | D03; D05..D07; D10; D11; A01; A02 | `not-created`; forbidden | FIX RED 2; FIX GREEN 2; exact 96; overflow reads 33; semantic/admission/provider/hook calls 0; Ruff/format/diff PASS | MRG1-RES-02 | MRG1-S06 | corrected candidate / settled re-review active |

### 26.8 MR0-C04 review RED — reject spoofed function signatures

The fresh `gpt-5.6-sol / max` settled-diff review verified the atomic-limit
correction, then found a separate major-severity merge blocker in static
control-shape inspection. `inspect.signature` honors a raw Python function's
custom `__signature__` even with `follow_wrapped=False` and `eval_str=False`.
An adapter class could therefore expose a real
`cancel(self, wrong_name, extra_positional)` implementation, attach a forged
compliant `__signature__`, and receive a conforming report even though
`cancel("id", reason="why")` immediately raises `TypeError`.

The controller independently reproduced both halves: static inspection
returned zero findings, while the real contract-shaped call failed. No class
construction or provider method invocation occurs in production conformance,
so that safety boundary remains intact; the defect is that untrusted signature
metadata can spoof the required five-method call shape.

The narrow correction must remain in the static inspector. A raw function
carrying custom `__signature__` metadata is rejected with the existing stable
`control_method_signature` code before `inspect.signature` is used. A
test-first regression must prove that the forged method is rejected, the
provider class remains unconstructed and uncalled, and ordinary compliant
methods continue to pass. No new finding code, API, dynamic call or annotation
inspection is authorized.

Nothing is staged, committed or pushed; the pre-correction `96 passed` remains
evidence but is not an accepted final GREEN.

| Entry ID | Decision / approval | Commit / push | Gate evidence | Residual | Snapshot | State |
|---|---|---|---|---|---|---|
| MR0-C04-E06 | D03; D05..D07; D10; D11; A01; A02 | `not-created`; forbidden | sol/max fresh review: atomic FIX PASS; 1 new major; controller spoof: static PASS / real call TypeError | MRG1-RES-02 | MRG1-S06 | review RED / signature-spoof FIX required |

### 26.9 MR0-C04 signature-spoof FIX GREEN

The `gpt-5.6-sol / high` coding subagent added the forged-signature regression
before changing production. Syntax passed; the targeted old-code run then
failed exactly once in `0.14s`. Before the failed conformance assertion, the
test proved that the real contract-shaped unbound `cancel` call raises
`TypeError` and that constructor/method counters remain zero. The old static
inspector nevertheless returned `conforms=True` with no findings.

The narrow correction retains `inspect.isfunction`, rejects a raw function
whose explicitly stored `__signature__` is non-null with the existing
`control_method_signature` code, and only then calls
`inspect.signature(..., follow_wrapped=False, eval_str=False)`. It neither
unwraps nor mutates the function and never constructs or invokes the provider.
The targeted GREEN covering the spoof, ordinary compliant control and existing
shape/authority negatives was `3 passed`; counters remained zero.

The exact C04 suite increased to `97 passed` (coding subagent `1.65s`,
controller `1.63s`). Scoped Ruff, five-file format and `git diff --check`
passed. Package initializer hashes remain unchanged; process cleanup was
empty. The new settled SHA-256 values are:

```text
src/vibecad/runtime/conformance.py
e7176e63f7b6966c2ddbddd35f822bd904fff0b863b39cfb5c38bc3b97b83d28
src/vibecad/interaction/cad_conformance.py
0a9df3e6aee7d04624b6bec68143525e635a7d01a559939ecd9e68ce63aae65d
tests/test_runtime_conformance.py
fd10a895493643377cd94ba35a180d2fed28233fc6cb8f9ebbf07e7a7e255060
tests/test_cad_runtime_conformance.py
aba53b4b42caab674cf20d601dc28efde75757c5596d16fbbceb8d390c25d5a7
tests/test_runtime_purity.py
04a118ffeae020da3de2f60959419d7f0123af2c718ed7bd0b7c0fcf836410f4
```

HEAD/upstream remain equal at
`71a25b583363fcbd3c4f8cf56c3cde594194e648`. The worktree contains only the
artifact plus the five C04 paths and has no staged content. A fresh independent
settled-diff review must now return `0/0/0/0`.

| Entry ID | Decision / approval | Commit / push | Gate evidence | Residual | Snapshot | State |
|---|---|---|---|---|---|---|
| MR0-C04-E07 | D03; D05..D07; D10; D11; A01; A02 | `not-created`; forbidden | spoof FIX RED 1; targeted GREEN 3; exact 97; constructor/method 0; Ruff/format/diff PASS | MRG1-RES-02 | MRG1-S06 | corrected candidate / fresh settled review required |

### 26.10 MR0-C04 settled-diff adversarial PASS

The independent `gpt-5.6-sol / max` reviewer re-read the final five-file
candidate and returned blocker/critical/major/minor `0/0/0/0`. It independently
ran four corrective regressions, the exact 97-test C04 suite, scoped Ruff,
format and diff checks. All passed.

The review confirmed that case overflow stops after the 33rd item with no
prepared cases, a single stable limit finding and zero generic semantics/CAD
admission/provider/hook activity. It also confirmed that non-null raw-function
`__signature__` metadata is rejected without unwrap, construction or
invocation while ordinary controls still conform.

The remainder of the full-scope review found no defect in stable/bounded
finding data, duplicate/hostile inputs, fake lifecycle evidence,
provider-free transcript evaluation, canonical admitted CAD routing,
unsupported exact-error handling, authority/artifact/selector/provenance
negatives, optimized mode, purity/imports or unchanged package initializers.
The reviewer reported no in-scope residual and no breaker. Its five source/test
hashes exactly matched Section 26.9; HEAD/upstream remained equal and the index
was empty.

The candidate may now enter an independent `gpt-5.6-terra / medium` pre-stage
mechanical gate. Exact staging remains forbidden until that gate passes.

| Entry ID | Decision / approval | Commit / push | Gate evidence | Residual | Snapshot | State |
|---|---|---|---|---|---|---|
| MR0-C04-E08 | D03; D05..D07; D10; D11; A01; A02 | `not-created`; forbidden | sol/max settled review 0/0/0/0; corrective 4; exact 97; Ruff/format/diff/hash/status PASS | none within C04; MRG1-RES-02 closes only on accepted push | MRG1-S06 | adversarial PASS / pre-stage mechanical gate next |

### 26.11 MR0-C04 independent pre-stage mechanical PASS

A supplementary `gpt-5.6-sol / max` corrective review also returned
blocker/critical/major/minor `0/0/0/0`, independently confirming exact
singleton findings and zero side-effect counters rather than weaker code-set
membership.

The independent `gpt-5.6-terra / medium` pre-stage gate then returned PASS:

- HEAD and upstream were equal at
  `71a25b583363fcbd3c4f8cf56c3cde594194e648`;
- the index was empty and status exactly matched the six-path C04 allowlist;
- the exact C04 suite was `97 passed in 1.88s`, with no warning or deselection;
- six signature/overflow/purity/import/optimized-mode regressions passed;
- scoped Ruff passed and all five files were already formatted;
- `git diff --check`, five settled hashes and both package initializer hashes
  matched;
- no pytest, FreeCAD or VibeCAD Worker process remained after excluding the
  process scanner itself.

The gate reported no residual and no breaker. Exact named staging of only the
artifact and five C04 paths is now authorized. A post-stage cached/unstaged
integrity gate remains mandatory before commit.

| Entry ID | Decision / approval | Commit / push | Gate evidence | Residual | Snapshot | State |
|---|---|---|---|---|---|---|
| MR0-C04-E09 | D03; D05..D07; D10; D11; A01; A02 | `not-created`; exact staging next | two final sol/max reviews 0/0/0/0; terra/medium pre-stage PASS; exact 97; corrective/purity 6; hashes/allowlist/index/leaks PASS | none within C04; MRG1-RES-02 closes only on accepted push | MRG1-S06 | gated / exact staging authorized |

### 26.12 MR0-C04 post-stage mechanical PASS

The same independent `gpt-5.6-terra / medium` gate verified the staged
candidate and returned PASS. The index contained exactly the artifact and five
C04 implementation/test paths with the expected modified/added status.
Unstaged and untracked path sets were empty, and cached/worktree SHA-256 values
matched for all six paths.

The cached candidate passed `git diff --cached --check`, the exact C04 suite
(`97 passed in 1.90s`), six corrective/purity/optimized-mode regressions,
scoped Ruff and format checks, both initializer hashes and final process
cleanup. HEAD/upstream remained equal at the C03 anchor. The gate reported no
residual and no breaker.

This evidence append changes only the controller-owned artifact. It must be
restaged by exact name, after which one cached-only hash/allowlist/diff
integrity check is required. No source or test may change before commit.

| Entry ID | Decision / approval | Commit / push | Gate evidence | Residual | Snapshot | State |
|---|---|---|---|---|---|---|
| MR0-C04-E10 | D03; D05..D07; D10; D11; A01; A02 | `not-created`; artifact restage next | terra/medium post-stage PASS; cached 6; unstaged/untracked 0/0; exact 97; corrective/purity 6; all-6 hash match | none within C04; MRG1-RES-02 closes only on accepted push | MRG1-S06 | post-stage gated / final cached integrity next |

## 27. MR0-C04 Acceptance and MR0-C05 Closeout Packet

### 27.1 MR0-C04 accepted commit

The final cached-only `gpt-5.6-terra / medium` gate passed after the Section
26.12 evidence append: the index contained exactly six paths with
`M/A/A/A/A/M`, unstaged and untracked sets were empty, cached/worktree hashes
matched, the artifact contained the post-stage evidence and
`git diff --cached --check` was clean.

The controller then created and immediately pushed:

```text
7c98e36c77ea748b2c33274d00d0f895ef3d8102
test(runtime): enforce adapter conformance
```

The accepted commit contains exactly the artifact and five C04 source/test
paths. Local HEAD and upstream resolved to the same full hash and the
post-push worktree was clean.

The accepted evidence includes the genuine missing-module RED, the initial
91/2 harness isolation correction, the strengthened unsupported route, the
atomic-overflow review RED/FIX and the forged-signature review RED/FIX. The
settled exact suite was `97 passed`; two final `gpt-5.6-sol / max` reviews
returned `0/0/0/0`, and independent pre-stage, post-stage and cached-integrity
mechanical gates passed.

`MRG1-RES-02` closed at this accepted push. C01 supplies immutable generic
runtime contracts and the deterministic descriptor registry; C04 supplies
reusable generic transcript conformance, CAD admission/snapshot conformance
and authority-negative tests. Closure means the internal foundation is
conformance-ready. It does not add result retrieval to `RuntimeControlPort`, a
live reconstruction/simulation provider, a second connected CAD adapter, a
public runtime wire schema, G1 UI, host verification, a tag or a release.

| Entry ID | Decision / approval | Commit / push | Gate evidence | Residual | Snapshot | State |
|---|---|---|---|---|---|---|
| MR0-C04-E11 | D01..D16; A01; A02 | `7c98e36c77ea748b2c33274d00d0f895ef3d8102`; pushed | exact 97; two reviews 0/0/0/0; terra/medium pre/post/final cached PASS; local/upstream equal | MRG1-RES-02 closed; RES-07 remains until a real second CAD adapter | MRG1-S07 | accepted / closed |

### 27.2 MR0-C05 read-only audit and clean-anchor baseline

At the C04 anchor, HEAD/upstream were equal, the index/worktree were clean and
the exact C05 four-path allowlist was sufficient:

```text
docs/ACCEPTANCE_TESTS.md
docs/ARCHITECTURE.md
docs/CAD_RUNTIME_ADAPTER_GUIDE.md
docs/orchestrated/vibecad-multi-runtime-g1.md
```

The documentation RED is real but non-behavioral:

- `docs/CAD_RUNTIME_ADAPTER_GUIDE.md` does not exist;
- `docs/ARCHITECTURE.md` still says C01..C04 require implementation and still
  places MR0 as the next milestone;
- `docs/ACCEPTANCE_TESTS.md` still labels C01..C04 as future gates rather than
  accepted internal foundation evidence.

`README.md` retains the pre-MR0 milestone wording recorded as
`MRG1-RES-10`. It is outside the approved C05 allowlist and is not a blocker
provided C05 claims consistency only for the canonical C05 documents and
carries the residual. Repository-wide documentation consistency would require
a separately approved allowlist expansion.

An independent `gpt-5.6-terra / medium` clean-anchor baseline passed:

```text
full non-slow: 5001 passed, 108 deselected, 19 warnings in 175.23s
exact C04:       97 passed in 1.93s
Ruff src/tests:  PASS
public surface:  28 tools / 6 operations
diff/status:     PASS / clean
```

No pytest process remained. No C05 file had been modified.

### 27.3 MR0-C05 seven-section implementation packet

#### 1. Authorization

MRG1-R1, D01..D16 and A01/A02 authorize the final documentation/evidence
closeout. This packet changes no executable source, test, package, version,
public schema, runtime behavior or external state. Architecture-sensitive
writing and review use `gpt-5.6-sol / max`; pure mechanical gates use
`gpt-5.6-terra / medium`. Controller-only actions remain artifact writes,
exact staging, commit and push.

#### 2. Workspace anchor and exact write scope

Start from pushed commit
`7c98e36c77ea748b2c33274d00d0f895ef3d8102` with a clean index/worktree.
The documentation implementer may edit only:

```text
docs/ACCEPTANCE_TESTS.md
docs/ARCHITECTURE.md
docs/CAD_RUNTIME_ADAPTER_GUIDE.md
```

This artifact is controller-owned. README, strategy/roadmap/agent docs,
source, tests, package/config and every other path are read-only. No worker may
stage, commit or push.

#### 3. Required documentation outcome

`ARCHITECTURE.md` and `ACCEPTANCE_TESTS.md` must replace C00-era future tense
with the accepted C01..C04 state while retaining all nonclaims: FreeCAD is the
only connected/default CAD adapter; public tools remain 28 and operations six;
the internal Python contracts are not a public SDK/wire schema; G1, host
verification, release, durable artifact migration and second CAD support are
not delivered.

Create `CAD_RUNTIME_ADAPTER_GUIDE.md` as the developer guide for the current
internal Python boundary. It must cover:

- exact generic identities, capabilities, descriptor registry, immutable
  invocation/budget/status/result/artifact/provenance values and the
  five-method `RuntimeControlPort`;
- exact CAD identity, five decision kinds, artifact profile, selector
  envelope, structural adapter, registry/router/domain service and no
  auto-discovery;
- a single runnable metadata/routing example which performs no CAD execution
  or persistence and is explicitly not product-support evidence;
- admission before snapshot conformance, deterministic fake lifecycle
  evidence and provider-free production transcript evaluation;
- the result-retrieval caveat: a supplied successful result is an observation,
  not a value retrieved through the current control port;
- exact bounds, atomic 33rd-case overflow, stable non-leaking findings and
  raw-function signature-spoof rejection;
- Task Kernel as the sole Task/Revision/lease/review/Accept/Reject/HEAD
  authority; structural authority checks are not an OS sandbox;
- semantic `SelectorV1` authority, optional runtime-qualified native locator
  and exact runtime/kind/media artifact qualification;
- the current FreeCAD-only default composition and descriptor
  (`cad/freecad@1.1.0`, headless, native FCStd plus exchange STEP);
- durable non-FreeCAD native storage remains blocked on MR1; conformance alone
  does not connect or support a second CAD.

Both canonical docs link the guide. The guide links back to both docs, this
artifact and the exact source/test modules using valid relative links.

#### 4. Steps and gates

Preserve the docs RED: missing guide plus exact stale statements. Implement
only the three documentation paths, then run:

- exact four-path status/allowlist and `git diff --check`;
- scoped relative Markdown links with zero broken targets;
- stale-state and product-overclaim searches;
- extraction/execution of the guide's single Python example under
  `PYTHONPATH=src`, without modifying the repository;
- the exact 97-test C04 suite;
- `PYTHONPATH=src .venv/bin/python -m pytest -q`, requiring exactly
  `5001 passed, 108 deselected, 19 warnings`;
- `.venv/bin/python -m ruff check src tests`;
- a canonical read-only probe requiring 28 public tools and six operations.

On settled document bytes, run the real gates exactly once:

```text
env -u VIBECAD_FREECAD_ENV \
  VIBECAD_MANAGED_FREECAD_PYTHON="/Users/wangtao/Library/Application Support/VibeCAD/runtime/mamba/envs/vibecad/bin/python" \
  PYTHONPATH=src .venv/bin/python -m pytest -q -m slow \
  tests/test_freecad_worker.py::test_real_managed_worker_load_modify_checkpoint_and_export

env -u VIBECAD_FREECAD_ENV \
  VIBECAD_RUN_INTEGRATION=1 PYTHONPATH=src \
  .venv/bin/python -m pytest -q -m slow \
  tests/test_task_kernel_integration.py::test_real_task_kernel_commits_verified_candidate \
  tests/test_task_kernel_integration.py::test_real_agent_first_public_matrix_and_cross_process_review
```

Both real commands require zero skip/deselection/warning and no process leak.
Require distinct architecture/product review and settled-diff adversarial
review, each `0/0/0/0`, then independent mechanical pre-stage PASS. After
exact named staging, post-stage work is cached allowlist/hash/diff/link/claim
integrity only; do not rerun full/real tests unless bytes changed.

#### 5. Execution discipline and breakers

No behavior TDD is required for a docs-only commit; the missing guide and
stale canonical claims are the accepted documentation RED. Stop on any need
for an out-of-allowlist edit, source/test/API correction, README closure,
second adapter composition, durable schema/storage migration, public support
claim, G1/host/release claim, broken link, example failure, test-count change,
new warning/deselection, real-gate skip/red, review finding or process leak.
Do not weaken an existing acceptance statement or use broad staging.

Terminology is fixed: managed runtime means the installer/supervisor
environment; generic runtime lifecycle means C01 contracts; CAD runtime
adapter means the internal selection/metadata boundary; provider means a
future read-only producer; supported/connected adapter requires real default
composition, engine evidence and product acceptance.

#### 6. Delivery boundary

The intended commit is exactly:

```text
docs(orchestration): close multi-runtime foundation
```

It contains only the three documentation paths and this artifact. C05 closes
the six-commit MR0 internal foundation, not G1, P0BH, HOST1, MR1, a second CAD,
a public protocol, tag or release. Exact staging, commit and immediate push
are controller-only.

The commit cannot contain its own final hash/push fact. The artifact records a
pre-closeout recovery snapshot and symbolic C05 acceptance; the controller's
post-push report supplies the actual hash. A seventh evidence-only commit is
forbidden by the six-commit budget.

#### 7. Required final report

Return exact changed paths/hashes, stale-claim and link results, example
execution, full/C04/public counts, Ruff/diff, real Worker/Task Kernel results,
review severities, pre/post-stage integrity, residuals/breakers, commit hash,
push result and final local/upstream/clean state. Explicitly carry
`MRG1-RES-10` and all product/durable residuals.

| Entry ID | Decision / approval | Commit / push | Gate evidence | Residual | Snapshot | State |
|---|---|---|---|---|---|---|
| MR0-C05-E01 | D01..D16; A01; A02 | `not-created`; forbidden | docs RED: guide absent + stale C00 claims; baseline 5001/108/19; C04 97; Ruff; 28/6; audit PASS | RES-01A/03/04/05/06A/07/08/09/10 | MRG1-S07 | packet issued / docs implementation next |

### 27.4 Recovery Snapshot MRG1-S07

#### Completed milestones

- C00 `6cc1876c8a76c9e3498262c78a0ad2c4ec6ddf6c`
- C01 `07c6d6cd0260dcce41711a4a92d47132460571db`
- C02 `6c3581bab14434ba7c1301e033e973d59907cc4d`
- C03 `71a25b583363fcbd3c4f8cf56c3cde594194e648`
- C04 `7c98e36c77ea748b2c33274d00d0f895ef3d8102`

All five commits were pushed. The current anchor is clean and equals upstream.
MRG1-RES-02 is closed. D01..D16 and A01/A02 remain active.

#### Current step

C05 may update only the two canonical docs, new adapter guide and this
artifact. Its docs RED and clean-anchor baseline are recorded in Sections
27.2–27.3. No implementation has begun.

#### Recovery commands

```text
git status --short
git rev-parse HEAD
git rev-parse @{upstream}
git log -6 --oneline
```

Resume only if HEAD/upstream equal the C04 anchor and status contains no path
outside the four-path allowlist. Re-run the exact C05 gates if document bytes
or the environment change.

#### Open residuals and next campaigns

- `MRG1-RES-01A`: durable Revision/Candidate/artifact storage remains fixed
  to FCStd/STEP; close in MR1.
- `MRG1-RES-03`: no real FreeCAD Qt Workbench UI; close in G1.
- `MRG1-RES-04`: no real Claude/Codex host verification; close in HOST1.
- `MRG1-RES-05`: P0-B hardening remains before P1 deliverable status.
- `MRG1-RES-06A`: mechanical research hypotheses remain unvalidated.
- `MRG1-RES-07`: no second real CAD adapter/engine/product acceptance.
- `MRG1-RES-08`: no reconstruction/simulation runtime.
- `MRG1-RES-09`: the public protocol does not expose runtime identity.
- `MRG1-RES-10`: README retains pre-MR0 milestone wording; close only under a
  separately approved documentation update.

After C05, G1, P0BH, HOST1 and MR1 are separate campaigns requiring their own
scope/approval. MR0 grants no authority to start them.

### 27.5 MR0-C05 documentation implementation GREEN

The `gpt-5.6-sol / max` architecture-writing subagent modified only the two
canonical documents and created the approved developer guide. It did not edit
this artifact, stage, commit or push.

The accepted documentation RED is closed:

- the adapter guide now exists;
- `ARCHITECTURE.md` describes C01..C04 as the implemented, conformance-gated
  internal foundation and no longer places MR0 as the next campaign;
- `ACCEPTANCE_TESTS.md` converts the future contract table into accepted
  evidence while separating generic fake lifecycle execution from fake CAD
  identity registration/routing.

The guide describes the exact internal contracts, authority boundary,
descriptor/admission/router flow, capability decisions, artifacts/selectors,
generic/CAD conformance, hard bounds, result-retrieval caveat, FreeCAD-only
default composition and MR1 durable breaker. It contains exactly one runnable
Python metadata/routing example; extracted execution under
`PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src` exited `0` with no output and did not
start CAD or write persistent state.

Controller and implementer checks agreed:

```text
relative links:       27 checked / 0 broken
Python fences:        1
example:              exit 0 / output 0
stale C00 claims:     0
guide whitespace:     PASS
git diff --check:     PASS
index:                empty
out-of-allowlist:     0
```

The three implementation SHA-256 values are:

```text
docs/ACCEPTANCE_TESTS.md
2bf6613ff266c07f1401b3248f8c04c91b093cc128787a4eccdf1887f6e0a881
docs/ARCHITECTURE.md
d208aeb4c8c67a4df269c4cbfd88b5078e946e86cc6af8f59fa8e8ada4a23509
docs/CAD_RUNTIME_ADAPTER_GUIDE.md
fa59c108f4a8142fbd82d94f46be3c6d8377433f695d44be94f9662a2050dc1c
```

The documents retain FreeCAD as the only connected/default adapter, 28 public
tools and six operations, and explicitly deny second-CAD support, a public
runtime SDK/schema, G1, host verification, durable migration and release.
`MRG1-RES-10` is carried rather than silently changing README. Full/real gates
have not yet been rerun on these bytes. Two distinct `gpt-5.6-sol / max`
reviews must return `0/0/0/0` before mechanical closeout.

| Entry ID | Decision / approval | Commit / push | Gate evidence | Residual | Snapshot | State |
|---|---|---|---|---|---|---|
| MR0-C05-E02 | D01..D16; A01; A02 | `not-created`; forbidden | docs RED closed; links 27/0; example 1/0; stale 0; diff/whitespace/scope PASS | RES-01A/03/04/05/06A/07/08/09/10 | MRG1-S07 | docs GREEN / independent reviews next |

### 27.6 MR0-C05 adversarial review RED — linked C00-era status

The first architecture review returned `0/0/0/0`, but the independent
adversarial reviewer found one closeout blocker. The updated architecture page
still linked `AGENT_ARCHITECTURE.md` and `PRODUCT_CAPABILITY_ROADMAP.md` as
product/roadmap references without disclosing that those pages retain
C00-era statements that C01..C04 are unfinished. `PRODUCT_STRATEGY.md` contains
the same stale completion status. That contradicts the current C05 pages if
the links are read as current MR0 completion-state authority.

This finding corrects the scope of two earlier records without rewriting
append-only history:

- Section 27.2's README-only audit was incomplete;
- Section 27.5's `stale C00 claims: 0` applies only to the three C05
  implementation documents, not the repository.

The exact out-of-scope stale clusters are:

```text
docs/AGENT_ARCHITECTURE.md
9-10, 36, 283-285, 397-399
docs/PRODUCT_STRATEGY.md
53, 79, 268-270, 692-693
docs/PRODUCT_CAPABILITY_ROADMAP.md
93-95
```

They are now tracked as `MRG1-RES-10A`: C00-era MR0 completion-state wording
in linked product/roadmap documents. Closure owner is a separately approved
documentation refresh. Repository-wide documentation consistency remains
open.

The adversarial reviewer ruled that C05 can still close within its approved
allowlist if both conditions are met:

1. `ARCHITECTURE.md` explicitly states that current C01..C04 completion status
   is governed by that page, `ACCEPTANCE_TESTS.md` and the adapter guide; the
   linked Agent/roadmap pages remain useful only for product positioning and
   longer-term capability content until RES-10A is closed.
2. All subsequent snapshot, review and final reports carry RES-10A and do not
   claim repository-wide or generalized stale-state closure.

No edit to the three stale files is authorized. Changing them now would be an
allowlist breaker. The narrow correction changes only `ARCHITECTURE.md` and
this append-only artifact, then requires fresh link/claim/hash checks and both
settled reviews.

| Entry ID | Decision / approval | Commit / push | Gate evidence | Residual | Snapshot | State |
|---|---|---|---|---|---|---|
| MR0-C05-E03 | D01..D16; A01; A02 | `not-created`; forbidden | architecture review 0/0/0/0; adversarial review 1 blocker / 0/0/0; linked stale clusters reproduced | RES-10 plus new RES-10A; repo-wide consistency OPEN | MRG1-S07 | review RED / scoped authority clarification required |

### 27.7 MR0-C05 linked-status FIX and settled review PASS

The architecture-writing subagent changed only the top reference paragraph in
`ARCHITECTURE.md`. Current C01..C04 completion status is now explicitly
governed by the architecture page, `ACCEPTANCE_TESTS.md` and the adapter guide.
The linked Agent and roadmap documents remain references only for product
positioning and longer-term capability content; their C00-era completion
wording is tracked by RES-10A and cannot override current status.

The narrow correction was `+6/-4`; the settled architecture SHA-256 became:

```text
fce53832e5cf1f232176fef30848086a26058086d15bfc6997433d9b3ca33c65
```

The three out-of-scope stale documents were not modified. Controller and
implementer checks passed with `28` relative links, zero broken links, zero
scoped stale claims, zero scoped overclaim matches, the runnable example
unchanged and `git diff --check` clean.

Both independent `gpt-5.6-sol / max` reviewers then re-read the frozen
candidate from zero and returned blocker/critical/major/minor `0/0/0/0`.
They confirmed:

- the prior canonical-status contradiction is closed within the approved
  allowlist;
- RES-10A covers the unambiguous stale completion-state clusters and
  repository-wide consistency remains OPEN;
- the artifact remains a strict byte-prefix append with no self-hash or
  seventh-commit claim;
- all current APIs, bounds, FreeCAD descriptor, authority/nonclaim language,
  28/6 public surface, links and example match source and tests.

The frozen implementation document hashes are:

```text
docs/ACCEPTANCE_TESTS.md
2bf6613ff266c07f1401b3248f8c04c91b093cc128787a4eccdf1887f6e0a881
docs/ARCHITECTURE.md
fce53832e5cf1f232176fef30848086a26058086d15bfc6997433d9b3ca33c65
docs/CAD_RUNTIME_ADAPTER_GUIDE.md
fa59c108f4a8142fbd82d94f46be3c6d8377433f695d44be94f9662a2050dc1c
```

No full or real gate has yet been run on the final document bytes. The
candidate may now enter the independent `gpt-5.6-terra / medium` pre-stage
mechanical gate. All subsequent evidence and reports must carry both RES-10
and RES-10A.

| Entry ID | Decision / approval | Commit / push | Gate evidence | Residual | Snapshot | State |
|---|---|---|---|---|---|---|
| MR0-C05-E04 | D01..D16; A01; A02 | `not-created`; forbidden | review RED 1 blocker; FIX +6/-4; fresh architecture/adversarial 0/0/0/0; links 28/0; example/diff/hash/scope PASS | RES-01A/03/04/05/06A/07/08/09/10/10A | MRG1-S07 | settled reviews PASS / pre-stage mechanical gate next |

### 27.8 MR0-C05 pre-stage mechanical gate RED — host-load timeout

The independent `gpt-5.6-terra / medium` pre-stage gate began from the frozen
Section 27.7 bytes. Its documentation/scope phase passed:

```text
anchor/status/index/hashes:       PASS
artifact strict HEAD-byte prefix: PASS
relative links:                   28 checked / 0 broken
Python fences:                    1
example:                          exit 0 / output 0
scoped stale claims:              0
RES-10 and RES-10A:               present
repo-wide consistency:            OPEN
diff/scope:                       PASS
```

The gate then stopped at the exact C04 suite. Three parametrizations of
`test_public_cad_iterables_are_bounded_without_trusting_length_hints` timed
out in their fixed `2.5`-second subprocess window for the declaration,
decision and adapter targets. The exact C04 invocation took more than
31 seconds instead of its prior approximately two-second settled runtime.
No later pre-stage phase was run.

The controller independently reran only those three parametrizations. That
probe returned `2 failed, 1 passed in 12.43s`; the declaration and adapter
subprocesses timed out. Crucially, captured stdout from both timed-out
subprocesses already contained:

```text
bounded-cad-iterable: PASS
```

Thus the required boundedness assertion completed before termination, while
subprocess exit/cleanup was not observed within the wall-clock deadline. At
the failure point the host was heavily scheduled; the first gate observed the
Codex app server near 85% CPU and Activity Monitor near 40% CPU. A later
controller sample still showed elevated host load. No pytest, FreeCAD or
VibeCAD worker leaked after either invocation.

All four C05 candidate paths retained their frozen bytes, the index remained
empty and nothing was staged, committed or pushed. This is retained as a
mechanical gate RED, not waived as an implementation pass. No test edit is
authorized by the C05 allowlist. A targeted stability probe may run only in a
demonstrably lower-load window; the full pre-stage gate may restart from the
beginning only after that probe passes consistently.

| Entry ID | Decision / approval | Commit / push | Gate evidence | Residual | Snapshot | State |
|---|---|---|---|---|---|---|
| MR0-C05-E05 | D01..D16; A01; A02 | `not-created`; forbidden | phase A PASS; exact C04 3 timeout failures; controller probe 2 failed/1 passed in 12.43s; timed-out stdout already PASS; no leaks | RES-01A/03/04/05/06A/07/08/09/10/10A | MRG1-S07 | mechanical RED / wait for lower-load stability probe |

### 27.9 MR0-C05 lower-load stability probe RED

The `gpt-5.6-terra / medium` gate subagent ran the authorized targeted
stability probe. Attempt one failed, so attempt two and the full pre-stage
gate were not run.

```text
baseline load:                 5.62 / 5.19 / 5.43
ending load:                   3.65 / 4.73 / 5.24
targeted result:               2 failed / 1 passed in 12.54s
declarations:                  TimeoutExpired(2.5s), stdout PASS
decisions:                     TimeoutExpired(2.5s), stdout PASS
adapters:                      PASS
stderr from timed-out children: empty
pytest/FreeCAD/VibeCAD leaks:  0
index:                         empty
git diff --check:              PASS
```

The failing targets changed from the controller probe, but the failure mode
did not: each timed-out child completed the bounded-iteration assertion and
printed the required success marker before its parent could observe full
process exit. The repeated RED makes the fixed `2.5`-second wall-clock
deadline unstable on the current host rather than a one-off gate incident.
It does not authorize a timeout waiver or a test change. Read-only exit-latency
measurement is the next diagnostic step; any change to the test requires an
explicit C05 allowlist expansion before implementation.

The three implementation-document hashes were unchanged. This artifact's
pre-probe and post-probe hash was
`ac187d3167f619475ea9027360247a4b940557c01375beff3ed78f9369e469db`;
the value is historical evidence for the pre-Section-27.9 bytes, not a
self-hash of the current artifact.

| Entry ID | Decision / approval | Commit / push | Gate evidence | Residual | Snapshot | State |
|---|---|---|---|---|---|---|
| MR0-C05-E06 | D01..D16; A01; A02 | `not-created`; forbidden | lower-load probe attempt 1: 2 failed/1 passed; both timeout stdout PASS; no leaks; hashes/diff/index PASS | RES-01A/03/04/05/06A/07/08/09/10/10A | MRG1-S07 | mechanical RED / characterize exit latency |

### 27.10 MR0-C05 timeout diagnosis and stability recovery GREEN

The same `gpt-5.6-terra / medium` gate subagent reproduced the exact child
logic outside pytest with a `20`-second safety ceiling and no workspace
writes. Six exact launch-to-exit measurements all returned `0`, printed the
required PASS marker and had empty stderr:

```text
declarations: 0.150550s / 0.153932s
decisions:    0.146165s / 0.147248s
adapters:     0.153203s / 0.146472s
```

A `flush=True` diagnostic variant separated success-marker time from process
exit:

```text
declarations: PASS at 0.142966s; exit +0.011851s; total 0.154817s
decisions:    PASS at 0.132767s; exit +0.011115s; total 0.143882s
adapters:     PASS at 0.132723s; exit +0.010859s; total 0.143582s
```

Three import-only child baselines were `0.146116s`, `0.154604s` and
`0.158567s`. Normal elapsed time is therefore almost entirely process startup
and import; post-assertion interpreter exit is approximately `11ms`. The
diagnostic found no implementation hang, background-thread wait or persistent
cleanup delay.

With starting load `2.78 / 3.53 / 4.54`, the authorized exact pytest stability
probe then passed twice consecutively:

```text
attempt 1: 3 passed in 0.60s; tool wall 0.875355s
attempt 2: 3 passed in 0.59s; tool wall 0.850537s
```

Ending load was `2.64 / 3.44 / 4.49`. No pytest, FreeCAD or VibeCAD worker
leaked. The three implementation-document hashes remained frozen, the index
was empty, the exact four-path worktree status was preserved and
`git diff --check` passed. This closes the stability-probe condition without
waiving the retained RED evidence or changing the test. The complete
pre-stage gate must still restart from its first phase and pass on the current
artifact bytes.

The artifact hash throughout the diagnosis and recovery probe was
`4ac99253dd02334408f0bcb41d92b98edbe38268c447282af758ac8219c99019`;
the value denotes the pre-Section-27.10 historical bytes.

| Entry ID | Decision / approval | Commit / push | Gate evidence | Residual | Snapshot | State |
|---|---|---|---|---|---|---|
| MR0-C05-E07 | D01..D16; A01; A02 | `not-created`; forbidden | six exact exits ~0.15s; post-PASS exit ~11ms; import baseline ~0.15s; stability probes 3/3 twice; no leaks | RES-01A/03/04/05/06A/07/08/09/10/10A | MRG1-S07 | timeout diagnosis GREEN / full pre-stage restart required |

### 27.11 MR0-C05 timeout adversarial disposition

A fresh read-only `gpt-5.6-sol / max` adversarial assessment found no product
hang or resource-leak evidence. The bounded CAD snapshot implementation uses a
deterministic `range(limit + 1)` plus `next()` ceiling: declarations and
decisions perform at most `1025` reads and retain at most `1024` values;
adapters perform at most `257` reads and retain at most `256` values. It starts
no implementation thread or subprocess. The contract intentionally does not
claim that an individual hostile `next()` call is a wall-clock sandbox.

Fifteen additional independent read-only child probes all returned `0` in
`0.138–0.190s`, showed only the non-daemon main thread and registered no
`atexit` callback. Together with parameter drift across failures, PASS stdout,
empty stderr, zero leaks, the Section 27.10 timings and consecutive recovery
GREEN, the evidence supports transient host scheduling rather than a product
loop or hidden cleanup wait.

The reviewer authorized exactly one complete pre-stage restart from phase A
without changing the test or C05 allowlist. This is not retry-until-green:
another occurrence of the same timeout is an immediate breaker requiring
explicit scope approval.

If that breaker occurs, the recommended minimum technical correction is an
independent test-only hotfix in `tests/test_cad_runtime.py`: retain all three
parameter children, flush the success marker and use `os._exit(0)` after the
boundedness assertion, while increasing the external watchdog to `10.0`
seconds. That preserves exact test counts and separates proven child semantics
from ordinary interpreter finalization. It is not authorized now. Combining
the parameters is rejected because it would reduce C04 from `97` to `95` and
the full selected count from `5001` to `4999`. A hotfix would also require an
explicit change from the approved six-commit plan; it cannot be hidden inside
the docs-only C05 commit or retroactively added to an already pushed commit.

Current severity is one process blocker, zero critical findings, zero major
product findings and one minor test-robustness risk. The process blocker clears
only when the complete pre-stage gate passes.

| Entry ID | Decision / approval | Commit / push | Gate evidence | Residual | Snapshot | State |
|---|---|---|---|---|---|---|
| MR0-C05-E08 | D01..D16; A01; A02 | `not-created`; forbidden | adversarial: no product hang/leak; 15 probes 0.138–0.190s; one full restart authorized; fallback not authorized | RES-01A/03/04/05/06A/07/08/09/10/10A; minor timeout robustness | MRG1-S07 | 1 process blocker / 0 critical / 0 major / 1 minor |

### 27.12 MR0-C05 complete pre-stage mechanical PASS

The authorized one-time `gpt-5.6-terra / medium` complete restart passed every
phase from the frozen Section 27.11 bytes. No failed phase was retried.

```text
A documentation/scope:
  exact status/index/hashes:             PASS
  artifact strict HEAD-byte prefix:      PASS
  relative links:                        28 checked / 0 broken
  Python fences / example:               1 / exit 0 / output 0
  scoped stale claims:                   0
  RES-10 / RES-10A / repo-wide OPEN:     present
  diff / process cleanup:                PASS

B exact C04:
  97 passed in 1.68s
  warning / deselection:                 0 / 0

C public surface:
  targeted tests:                        2 passed in 0.21s
  canonical projection:                  28 tools / 6 operations

D Ruff:
  src + tests:                           PASS

E full default:
  5001 passed, 108 deselected, 19 warnings in 142.31s
  failure / error:                       0 / 0

F real managed FreeCAD:
  Worker:                                1 passed in 1.98s
  Task Kernel:                           2 passed in 17.42s
  skip / deselection / warning:          0 / 0 / 0

G final integrity:
  four frozen hashes / exact status:     PASS
  index / diff / artifact prefix:        empty / PASS / PASS
  HEAD / upstream:                       7c98e36... / 7c98e36...
  pytest/FreeCAD/Worker/daemon leaks:    0
```

The exact settled pre-evidence artifact hash was
`3838a0a3b725e88b88a82ec27882b4e0b25cbacc658b8ef717b4f382c384a7e6`.
All open residuals remain explicit. The Section 27.11 process blocker is
closed; the minor timeout-robustness observation remains recorded without a
test change or waiver.

Exact named staging of only the three documentation paths and this artifact is
now mechanically authorized. Post-stage verification is cached
allowlist/hash/diff/link/claim integrity only; full, C04 and real gates must
not be rerun unless candidate bytes outside an evidence append change.

| Entry ID | Decision / approval | Commit / push | Gate evidence | Residual | Snapshot | State |
|---|---|---|---|---|---|---|
| MR0-C05-E09 | D01..D16; A01; A02 | `not-created`; exact staging next | A–G PASS; C04 97; public 2 + 28/6; Ruff; full 5001/108/19; real Worker 1; Task Kernel 2; zero leaks | RES-01A/03/04/05/06A/07/08/09/10/10A; minor timeout robustness | MRG1-S07 | pre-stage PASS / no breaker |

### 27.13 Post-MR0 product steering — FreeCAD E2E plus parallel MR1

The user set the post-MR0 product priority:

- a second CAD is not urgent; MR0's internal adapter boundary and conformance
  reservation are sufficient for now;
- FreeCAD end-to-end product capability is the primary path;
- G1 and MR1 should run as coordinated parallel campaigns;
- P0-B hardening and real Claude/Codex host verification remain supporting
  tracks.

The intended FreeCAD product chain is:

```text
Claude/Codex -> MCP/skill -> Task Kernel -> CAD Domain/FreeCAD adapter
-> managed Worker -> candidate/verifier -> durable draft/revision
-> FreeCAD Workbench preview and Accept/Reject -> FCStd/STEP delivery
```

G1 owns the Workbench/public-client experience and must not read or write
Revision directories directly. MR1 owns durable schema migration,
runtime/profile artifact identity, v1 compatibility, recovery and rollback;
it does not connect a second CAD or expand G1's UI scope. G1 alpha may validate
the existing v1 FCStd/STEP path, but external beta, non-disposable user data
and release require a shared G1/MR1 integration gate. The gate must cover old
v1 projects and new/migrated data through preview, draft, verdict,
Accept/Reject, artifact retrieval, restart/reconcile and rollback.

This is approved product direction, not an expansion of MR0-C05. G1 and MR1
require separate exact allowlists, commit sequences and recovery packets
before their source implementation begins. RES-07 remains deliberately open;
RES-01A becomes the parallel MR1 closure target after C05.

| Entry ID | Decision / approval | Commit / push | Gate evidence | Residual | Snapshot | State |
|---|---|---|---|---|---|---|
| MR0-C05-E10 | user post-MR0 steering | `not-created`; no MR0 scope expansion | G1/MR1 common beta/release gate required; second CAD deferred | RES-01A moves to parallel MR1; RES-07 intentionally open; P0BH/HOST1 remain | MRG1-S07 | future direction approved / exact campaign packets required |

### 27.14 Post-MR0 MR1 deferral-risk audit

A read-only `gpt-5.6-sol / max` source audit confirmed the approved parallel
direction. G1 alpha need not wait for complete MR1 if its data is explicitly
resettable or exportable. MR1 must join before the first promise that an
external user's durable project will survive product upgrades; a tag alone is
not the governing threshold.

The audit found strict v1 durable coupling across Revision manifests, HEAD,
journals, drafts, verification reports, task artifacts, delivery eligibility
and materialization. Decoders reject non-v1 or unknown record fields, checksum
domains are v1-specific, and manifest byte digests are cross-bound into those
records. Fixed `model.FCStd`/`model.step` assumptions occur broadly across the
revision store, execution, artifact delivery and public manifest validation.
An in-place manifest rewrite would invalidate existing receipts and recovery
facts.

The required MR1 strategy is therefore:

- preserve immutable v1 revisions and interpret an absent v1 profile only as
  the exact FreeCAD FCStd/STEP profile;
- add profile codec dispatch and dual-version readers before activating a v2
  writer;
- allow mixed v1/v2 ancestry while keeping downgrade writes fail-closed;
- inventory projects, revisions, lineage, active journals/candidates,
  drafts/checkouts and materializations before any migration;
- exercise crash, restart, reconcile, rollback and backup restore rather than
  bulk rewriting old revisions.

The existing opaque `vibecad://artifact/{materialization_id}/{artifact_id}`
resource URI is a safe G1/MR1 seam. G1 must use application APIs and file
grants; it must not parse `manifest.json`, construct revision paths, rely on
artifact list positions or open an internal `model.FCStd` path.

Before complete MR1, a narrow preparation packet should freeze a byte-level v1
golden corpus and a read-only migration inventory/preflight. The shared
G1/MR1 beta gate must prove v1 readability, mixed v1-to-v2 lineage,
draft Accept/Reject, revert, restart/reconcile, G1 preview/grant/revocation,
opaque-URI compatibility, backup restore and fail-closed old-writer behavior.

Complete MR1 may no longer be deferred after any of these triggers:

- the first promised non-disposable external project;
- the first non-FreeCAD durable profile or native format;
- a FreeCAD feature requiring artifact cardinality beyond fixed FCStd/STEP;
- any G1 dependency on an internal durable path;
- an ambiguous recovery state or a migration dry-run exceeding its maintenance
  window.

As engineering warning thresholds rather than product contracts, a real
snapshot dry-run should be forced by any of `100` projects, `1,000` revisions
or `10 GiB` of v1 data.

| Entry ID | Decision / approval | Commit / push | Gate evidence | Residual | Snapshot | State |
|---|---|---|---|---|---|---|
| MR0-C05-E11 | approved G1/MR1 parallel direction | `not-created`; no MR0 scope expansion | sol/max durable audit; immutable-v1 + dual-reader/new-v2; opaque URI seam; beta triggers fixed | RES-01A parallel closure target; RES-07 deferred | MRG1-S07 | handoff guardrails accepted / C05 exact staging next |

### 27.15 MR0-C05 post-stage cached mechanical PASS

The exact named stage contained only:

```text
M  docs/ACCEPTANCE_TESTS.md
M  docs/ARCHITECTURE.md
A  docs/CAD_RUNTIME_ADAPTER_GUIDE.md
M  docs/orchestrated/vibecad-multi-runtime-g1.md
```

The independent `gpt-5.6-terra / medium` cached-only gate passed. There were
zero unstaged and zero untracked paths. Cached and worktree SHA-256 values
matched for all four paths; the pre-evidence artifact value was
`9e2aa22b90d30315d862252b2ae3f37b801d4cc15acbec1d96fc84de9683b746`.
`git diff --cached --check` passed, and both cached and worktree artifact bytes
were strict appends to the HEAD artifact.

Cached documentation checks returned `28` relative links with zero broken
targets, one guide Python fence without re-execution, zero scoped stale claims
and zero positive product overclaims. Two lexical overclaim candidates were
both explicit negative statements denying second-CAD support. All residuals,
RES-10A, repository-wide consistency OPEN, FreeCAD-only support, second-CAD
non-delivery and the future/no-MR0-expansion status of G1/MR1 were retained.

The artifact contained no current self-hash, no positive current-C05
commit/push fact and no positive seventh-commit claim. HEAD and upstream both
remained `7c98e36c77ea748b2c33274d00d0f895ef3d8102`; no pytest, FreeCAD, Worker
or daemon process leaked. As required, the gate did not rerun the guide
example, full suite, exact C04 suite or real gates.

This evidence append changes only the controller-owned artifact. Exact
restaging of that path followed by one final cached
allowlist/hash/diff/prefix check is required before creating C05.

| Entry ID | Decision / approval | Commit / push | Gate evidence | Residual | Snapshot | State |
|---|---|---|---|---|---|---|
| MR0-C05-E12 | D01..D16; A01; A02; post-MR0 steering | `not-created`; artifact restage next | cached M/M/A/M; unstaged/untracked 0/0; hashes/links/claims/prefix/diff/leaks PASS | RES-01A/03/04/05/06A/07/08/09/10/10A; minor timeout robustness | MRG1-S07 | post-stage PASS / final cached integrity next |
