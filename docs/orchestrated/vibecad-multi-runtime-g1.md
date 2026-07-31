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

## 28. MRG1-R2 G1/MR1 Parallel Execution Contract

This section is an append-only successor to `MRG1-R1`. It records the
completed MR0 closeout, the user's post-MR0 product steering, three independent
read-only `gpt-5.6-sol / max` audits and the proposed exact first
implementation campaigns for G1 and MR1. It does not rewrite the historical
R1 header.

### 28.1 MR0 closeout and authorization MRG1-A03

MR0 completed as the approved six-commit first-parent chain:

```text
6cc1876 docs(architecture): define multi-runtime CAD boundary
07c6d6 feat(runtime): add runtime capability contracts
6c3581b feat(cad): add backend-neutral CAD runtime port
71a25b5 refactor(freecad): route worker through CAD runtime adapter
7c98e36 test(runtime): enforce adapter conformance
2de1a37 docs(orchestration): close multi-runtime foundation
```

`2de1a37cc7e67268965a5a7b9519b2bf0e049f9a` is pushed to
`origin/codex/agent-stage3`. Its final gates were C04 `97 passed`, public
surface `2 passed` plus `28 tools / 6 operations`, Ruff PASS, full default
`5001 passed, 108 deselected, 19 warnings`, real managed Worker `1 passed`,
real Task Kernel `2 passed` and zero leaked pytest, FreeCAD, Worker or daemon
processes.

The user first directed that a second CAD is not urgent, the architecture
reservation is sufficient and FreeCAD end-to-end product capability is the
priority. After the controller explained that G1 and MR1 can run in parallel
provided they join before durable external beta, the user granted:

| Approval | Timestamp | Artifact | Scope | Exact user text | State |
|---|---|---|---|---|---|
| MRG1-A03 | 2026-07-26T07:33:57Z | MRG1-R2 direction | G1 FreeCAD alpha plus parallel MR1; common non-disposable beta gate | `那就按照这样来吧` | product direction approved / exact R2 implementation pending |

MRG1-A03 carries forward MRG1-A01's product decisions and MRG1-A02's
subagent routing. It approves parallel product direction, but cannot
retroactively authorize allowlists and gates that had not yet been produced or
shown when the user granted it. The exact campaigns below require a new
MRG1-A04 after this revised packet is shown. Until A04, only this
controller-owned artifact may be verified, committed and pushed; no G1 or MR1
source implementation may start.

MRG1-A04 is reserved for the user's explicit approval of Sections 28.2–28.8,
including the exact initial commit budget, the conditional daemon-bootstrap
allowlist and the shared non-disposable beta gate. Neither A03 nor a future
A04 authorizes second-CAD implementation, face/edge selectors, semantic diff,
manual checkout publication, an Addon Manager release, a tag/release, external
credentials or spend.

Stable decisions introduced by R2 are:

- `MRG1-D17` — FreeCAD end-to-end product capability precedes a second CAD;
  adapter/conformance reservation remains sufficient. Active under A03.
- `MRG1-D18` — G1 and MR1 run in isolated parallel write domains and join
  before non-disposable external beta. Active under A03.
- `MRG1-D19` — real Workbench alpha may use disposable/exportable v1 data, but
  cannot claim durable beta before the shared gate. Proposed under A04.
- `MRG1-D20` — MR1-prep reports observational `structurally_ready`; only a
  future fenced second scan can report `activation_ready`. Proposed under A04.
- `MRG1-D21` — exact named staging, one-commit index ownership and
  artifact-first persistence apply to every authorization and correction.
  Proposed under A04.
- `MRG1-D22` — the adapter routes `fast` to terra/medium mechanical work,
  `standard` to sol/high ordinary coding and `deep` to sol/max durable
  architecture/adversarial work. Proposed under A04.

### 28.2 Product-meaningful first outcome

The first user-visible vertical slice is one real managed FreeCAD 1.1
Workbench:

1. FreeCAD discovers and activates one VibeCAD Workbench and Dock.
2. The Dock opens its own `LocalAgentClient` session to the one authenticated
   local Kernel daemon.
3. The user chooses one managed project and one
   `awaiting_user_review` task.
4. HEAD and draft are opened through `checkout.open`, same-session one-shot
   `file_grant.claim` and two separate non-authoritative Preview Documents.
5. The Dock shows the authoritative verification verdict plus
   live/stale/revoked/recovery-required and local dirty state.
6. Accept or Reject uses the latest exact task id, draft id and generation,
   then re-reads Task and HEAD.
7. Whole-object or feature selection from a managed Preview Document produces
   an exact `SelectorV1` mapping for copy to the host.

The Dock is a thin client. It never owns Task, Revision, HEAD, lease, candidate
or review authority; never reads `manifest.json`, constructs a Revision path,
selects an artifact by list position or opens an internal `model.FCStd`;
never saves or publishes a Preview Document; and never treats `Name`, `Label`,
`FaceN` or `EdgeN` as a durable selector.

G1 may call this result an alpha only while its data is explicitly disposable
or independently exportable. It cannot be called durable beta or be offered
with a promise that user projects survive upgrades until the shared gate in
Section 28.7 is GREEN.

This is the final G1 vertical-slice target, not a claim that the initial
post-A04 budget alone delivers every item. The first tranche closes addon
registration, isolated embedded bootstrap, Dock/client threading,
preview/review behavior and selector capture in repository/real-GUI gates.
Deterministic user installation/launch packaging and the full real
Accept/Reject integration commit receive exact follow-on allowlists after
G1-M00; they are required before the vertical slice is called complete.

### 28.3 Parallel write-domain lock

G1 and MR1 may run concurrently only with these ownership rules:

- G1 owns new `freecad/VibeCAD/**` Workbench files and their dedicated tests.
  It treats the Application, daemon, protocol, checkout, grant, selector and
  durable-store implementations as read-only facades.
- MR1 owns durable revision codec, byte corpus and migration inventory work.
  It does not modify any Workbench file.
- `src/vibecad/daemon/{adapters,facade}.py`,
  `src/vibecad/application/{project_api,task_api}.py`,
  `src/vibecad/interaction/{checkouts,file_grants}.py` and
  `src/vibecad/execution/selectors.py` are shared seams. Neither parallel track
  may change them except where an exact commit below names the path.
- `pyproject.toml`, `uv.lock`, `manifest.json`,
  `.github/workflows/release.yml`, canonical release documentation and shared
  acceptance/integration tests have one controller-owned serial integration
  writer.
- This artifact is the one intentional shared path in every exact commit
  allowlist. Only the controller appends its ledger/recovery rows after a
  subagent returns candidate bytes; coding/research/gate subagents never edit
  it. Parallel development therefore converges through serial
  ledger-append -> exact stage -> commit -> push cycles.
- Any need for both tracks to edit the same unnamed source path is a breaker.
  The controller must stop, preserve both candidate diffs and approve a new
  serial integration commit rather than merging opportunistically.

### 28.4 G1 exact first implementation campaign

Each behavior-changing G1 commit is developed from a genuine focused RED to
GREEN; RED evidence is recorded before production bytes are written, but no
commit may leave the branch failing. A characterization-only branch may record
the already-correct real behavior as its first GREEN. If that characterization
exposes a defect and production bytes change, the reproduced failure is the
required RED.

#### G1-C00P — Expose the selected GUI binary path

Subject:

```text
feat(runtime): expose managed FreeCAD GUI path
```

Exact controller-owned serial allowlist:

```text
M docs/orchestrated/vibecad-multi-runtime-g1.md
M src/vibecad/runtime/paths.py
M tests/test_paths.py
```

The pure path helper selects the active runtime prefix and constructs only its
platform-specific FreeCAD GUI binary path. It does not claim that an override
or prefix is verified, start/install/probe FreeCAD or change
`freecadcmd_path()`. Every real GUI caller must separately validate existing
receipt/runtime-generation evidence, the prefix identity and the GUI binary's
regular-file identity and execute permission.

#### G1-C00B — Prove embedded daemon bootstrap

This commit has two mutually exclusive observable branches:

```text
GREEN probe:
  test(workbench): prove embedded daemon bootstrap

reproduced RED plus narrow correction:
  fix(daemon): bind embedded bootstrap to managed Python
```

Exact allowlist:

```text
M docs/orchestrated/vibecad-multi-runtime-g1.md
A tests/fixtures/freecad_workbench/bootstrap_probe.py
A tests/test_freecad_workbench_bootstrap.py
M src/vibecad/daemon/bootstrap.py
M tests/test_p0b_acceptance.py
```

The owner-private, bounded real-FreeCAD probe runs before any addon or Dock is
written. Its invocation includes `-P <repo>/src` and
`-P <repo>/tests/fixtures/freecad_workbench`; it asserts the exact
`vibecad.__file__` and `daemon/bootstrap.py` source identities, records
`sys.executable`, cold-opens one isolated `LocalAgentClient`, records the daemon
id and closes the client. The parent then calls bounded
`retire_local_kernel(reason="runtime_upgrade", expected_daemon_id=<recorded>)`
against the isolated run root and proves the recorded pid is dead, the socket
is absent and no daemon process or live run-root identity remains. Client close
alone is not cleanup evidence.

If the current `[sys.executable, -B, -m, vibecad.daemon]` works, apart from the
controller artifact only the two new test paths are present at commit. If and
only if the real probe reproduces the embedded-launch failure, the same
RED/GREEN commit may add the two named production/regression paths and bind
daemon launch to the verified active managed Python with a development-Python
fallback. It cannot create a second daemon, Application or Task Kernel. Any
other path or failure mode is a breaker. This branch completes and the index
returns empty before G1-C00 or C01 begins; no stash or mixed allowlist is
permitted.

#### G1-C00 — Register the thin-client addon

Subject:

```text
feat(workbench): register thin-client FreeCAD addon
```

Exact allowlist:

```text
M docs/orchestrated/vibecad-multi-runtime-g1.md
A freecad/VibeCAD/Init.py
A freecad/VibeCAD/InitGui.py
A freecad/VibeCAD/package.xml
A freecad/VibeCAD/vibecad_workbench/__init__.py
A freecad/VibeCAD/vibecad_workbench/state.py
A tests/fixtures/freecad_workbench/fake_host.py
A tests/test_freecad_workbench_package.py
A tests/test_freecad_workbench_controller.py
```

RED requires the absent classic `Mod/VibeCAD` addon and presenter contracts to
fail. GREEN requires `Init.py` to be headless/no-op, `InitGui.py` to register
exactly once without connecting to the daemon at import time, and `state.py`
to remain a Qt-, FreeCAD- and store-independent projection of public mappings.

#### G1-C01 — Connect a responsive review Dock

Subject:

```text
feat(workbench): connect review dock to public kernel
```

Exact allowlist:

```text
M docs/orchestrated/vibecad-multi-runtime-g1.md
M freecad/VibeCAD/InitGui.py
A freecad/VibeCAD/vibecad_workbench/gateway.py
A freecad/VibeCAD/vibecad_workbench/dock.py
A freecad/VibeCAD/vibecad_workbench/host.py
M tests/fixtures/freecad_workbench/fake_host.py
A tests/fixtures/freecad_workbench/gui_harness.py
M tests/test_freecad_workbench_controller.py
A tests/test_freecad_workbench_gui.py
```

One dedicated Qt worker thread owns one `LocalAgentClient`; connect, discovery,
refresh and review RPC run there. All `FreeCADGui`, document, selection and
widget operations run on the Qt main thread through queued signals carrying
plain mappings. A blocking RPC on the GUI thread or a worker touching a GUI
object is a breaker. The GUI harness is opt-in and bounded, drives the real
managed `freecad` binary, emits one machine-readable result and is required
GREEN before C01 can be committed.

#### G1-C02 — Open safe HEAD and draft previews

Subject:

```text
feat(workbench): preview managed head and draft
```

Exact allowlist:

```text
M docs/orchestrated/vibecad-multi-runtime-g1.md
M freecad/VibeCAD/vibecad_workbench/state.py
M freecad/VibeCAD/vibecad_workbench/gateway.py
M freecad/VibeCAD/vibecad_workbench/dock.py
M freecad/VibeCAD/vibecad_workbench/host.py
A freecad/VibeCAD/vibecad_workbench/preview.py
M tests/fixtures/freecad_workbench/fake_host.py
M tests/test_freecad_workbench_controller.py
A tests/test_freecad_workbench_preview.py
```

Each source uses `checkout.open` followed immediately by a same-session,
one-shot grant claim. The main thread opens only the claimed exact path.
Descriptor, checkout and document identities remain bound until close, in
document -> checkout -> client order. `get_checkout` dirty/stale/revoked state
or `Document.Modified` disables Accept and requires discard/close/reopen before
review. Workbench code never calls save or save-as and a user-triggered save
can affect only the non-authoritative checkout: it is never published or used
for the old verdict. The preview cannot guess a path or reuse a grant.

#### G1-C03 — Review with fresh authority

Subject:

```text
feat(workbench): accept or reject reviewed draft
```

Exact allowlist:

```text
M docs/orchestrated/vibecad-multi-runtime-g1.md
M freecad/VibeCAD/vibecad_workbench/state.py
M freecad/VibeCAD/vibecad_workbench/gateway.py
M freecad/VibeCAD/vibecad_workbench/dock.py
M freecad/VibeCAD/vibecad_workbench/preview.py
M tests/fixtures/freecad_workbench/fake_host.py
M tests/test_freecad_workbench_controller.py
M tests/test_freecad_workbench_preview.py
A tests/test_freecad_workbench_review.py
```

Accept first re-reads Task and checkout, and is enabled only for the latest
`awaiting_user_review`, live, disk-clean and unmodified draft. Reject also
uses the latest awaiting-review generation and remains HEAD-neutral. A timeout
or disconnect has unknown outcome: reconnect and read durable state; never
blindly replay a decision. Success re-reads Task and Project and closes both
previews.

#### G1-C04 — Capture Level-A selectors

Subject:

```text
feat(workbench): capture managed object selectors
```

Exact allowlist:

```text
M docs/orchestrated/vibecad-multi-runtime-g1.md
M freecad/VibeCAD/vibecad_workbench/state.py
M freecad/VibeCAD/vibecad_workbench/dock.py
M freecad/VibeCAD/vibecad_workbench/host.py
A freecad/VibeCAD/vibecad_workbench/selection.py
M tests/fixtures/freecad_workbench/fake_host.py
M tests/test_freecad_workbench_controller.py
A tests/test_freecad_workbench_selection.py
```

Only a whole managed DocumentObject in a tracked Preview Document is accepted.
A managed object carrying a `feature_id` yields a feature-entity selector; this
does not select a Face or Edge subelement. Project and revision ids come only
from the live checkout binding, never a widget or object label.
`parse_entity_identity()` and `EntityIdentity.to_selector()` remain the sole
identity-construction path, followed by `resolve_selector()` over that tracked
document's complete `Document.Objects`; the result must be the same selected
object and unique. Any subelement, missing or malformed VibeCAD metadata,
revision mismatch or ambiguity fails closed.

#### G1-M00 — Real GUI bootstrap breaker gate

Before C01 grows beyond its minimal Dock, the controller must run the actual
managed `freecad` GUI, not `freecadcmd` or mocked Qt, with an owner-private
temporary root and the exact `freecad/VibeCAD` module. The bounded harness is
`tests/fixtures/freecad_workbench/gui_harness.py`, asserted by
`tests/test_freecad_workbench_gui.py`. Before process launch, the controller
must use existing receipt/runtime-generation verification to bind the selected
prefix identity and GUI regular-file identity/owner/mode, and record that
evidence. Its normalized invocation is:

```text
VIBECAD_HOME=<0700-temp>/vibecad
VIBECAD_FREECAD_ENV=<verified-managed-prefix>
FREECAD_USER_HOME=<0700-temp>/freecad-home
FREECAD_USER_DATA=<0700-temp>/freecad-data
FREECAD_USER_TEMP=<0700-temp>/freecad-temp
<verified-managed-prefix>/bin/freecad
  -M <repo>/freecad
  -P <repo>/src
  -P <repo>/tests/fixtures/freecad_workbench
  --run-test gui_harness
```

The platform-specific GUI binary comes from a validated runtime path helper,
not a guessed command. The harness has one absolute timeout, closes every
window it owns and may touch only the temporary VibeCAD durable root and
temporary FreeCAD configuration/data/temp roots. It must record:

- addon scan, one Workbench registration and Dock activation;
- `sys.executable`, Qt binding/version and main/worker thread identities;
- a cold `LocalAgentClient` open and daemon id;
- exact `vibecad.__file__` and daemon-bootstrap source identities under the
  reviewed repository `src/` tree;
- a responsive Qt heartbeat while connecting and refreshing;
- clean GUI shutdown with no dangling thread, session, grant or checkout;
- bounded retirement by the exact recorded daemon id, followed by proof that
  its pid, socket, process and live run-root identity are absent.

One audit-only invocation of the managed GUI binary with `--help` unexpectedly
entered Qt startup on this host, reported a missing optional
`3DconnexionNavlib` framework, opened message-box paths and required SIGINT.
It changed no repository byte and is not M00 gate evidence. The isolated
harness must not assume `--help` is headless; an unhandled startup modal or
GUI timeout is an M00 breaker and its process must be reclaimed.

The embedded `sys.executable` breaker and its only authorized correction are
resolved earlier by G1-C00B. M00 cannot reopen or widen that correction.

Later launcher, deterministic addon packaging, real Accept/Reject E2E and
canonical documentation are controller-owned integration commits whose exact
allowlists will be frozen only after G1-M00 proves the real discovery and
process model. Addon Manager publication is outside G1.

### 28.5 MR1-prep exact first implementation campaign

MR1-prep creates no v2 durable bytes, migration marker or second-CAD support.
The current writer remains byte-exact v1 throughout P00..P03.

#### MR1-P00 — Freeze the durable-v2 migration contract

Subject:

```text
docs(mr1): freeze durable-v2 migration contract
```

Exact allowlist:

```text
M docs/orchestrated/vibecad-multi-runtime-g1.md
M docs/ARCHITECTURE.md
M docs/ACCEPTANCE_TESTS.md
M docs/CAD_RUNTIME_ADAPTER_GUIDE.md
A docs/orchestrated/vibecad-durable-v2.md
```

The contract requires immutable v1, dual-reader before new-write-v2, mixed
ancestry, fail-closed downgrade, inventory/preflight, backup/restore and
rollback. An absent v1 profile maps only to the fixed legacy FreeCAD
FCStd/STEP profile. It must not serialize internal `CadArtifactProfile` or
`RuntimeDescriptor` as a public/durable schema by convenience.

P00 is a documentation G0, not a behavior test: its prewrite evidence is the
audited absence of a v1 byte corpus, codec dispatch, full-root inventory and
activation/rollback contract. It does not manufacture a failing pytest.
MR1-G00 is its postwrite integrity/consistency GREEN.

#### MR1-P01 — Freeze the byte-exact v1 corpus

Subject:

```text
test(durable): freeze byte-exact v1 golden corpus
```

Exact allowlist:

```text
M docs/orchestrated/vibecad-multi-runtime-g1.md
A tests/fixtures/durable_v1/index.json
A tests/fixtures/durable_v1/generation_zero_empty_manifest.json
A tests/fixtures/durable_v1/generation_zero_empty_head.json
A tests/fixtures/durable_v1/generation_zero_import_manifest.json
A tests/fixtures/durable_v1/generation_zero_import_head.json
A tests/fixtures/durable_v1/sealed_revision_manifest.json
A tests/fixtures/durable_v1/sealed_revision_head.json
A tests/fixtures/durable_v1/journal_staging.json
A tests/fixtures/durable_v1/journal_prepared.json
A tests/fixtures/durable_v1/journal_committed.json
A tests/fixtures/durable_v1/journal_not_committed.json
A tests/fixtures/durable_v1/reservation.json
A tests/fixtures/durable_v1/seed_intent.json
A tests/fixtures/durable_v1/seed_binding.json
A tests/fixtures/durable_v1/task_active.json
A tests/fixtures/durable_v1/task_awaiting_review.json
A tests/fixtures/durable_v1/task_succeeded.json
A tests/fixtures/durable_v1/task_failed.json
A tests/fixtures/durable_v1/task_rejected.json
A tests/fixtures/durable_v1/task_cancelled.json
A tests/fixtures/durable_v1/materialization_request.json
A tests/fixtures/durable_v1/materialization_delivery.json
A tests/fixtures/durable_v1/project_create_hmac_key.json
A tests/fixtures/durable_v1/project_create_request.json
A tests/fixtures/durable_v1/project_create_quarantine_receipt.json
A tests/fixtures/durable_v1/checkout_open_v1.json
A tests/fixtures/durable_v1/checkout_tombstone_v1.json
A tests/fixtures/durable_v1/checkout_open_v2.json
A tests/fixtures/durable_v1/checkout_tombstone_v2.json
A tests/fixtures/durable_v1/model.FCStd
A tests/fixtures/durable_v1/model.step
A tests/test_durable_v1_corpus.py
```

The indexed corpus covers empty/imported generation zero, a sealed FCStd/STEP
Revision, HEAD, all journal decisions, reservation/seed records, active and
terminal Task states, draft/report/artifact digest cross-binding,
materialization request/delivery, project-create request/HMAC/quarantine
records, legacy managed-checkout v1 open/tombstone facts and the current
managed-checkout v2 writer's open/tombstone facts.
Fixture SHA-256, size and canonical bytes are fixed before production
refactoring. Normal tests have no update-golden mode and may not generate a
fixture immediately before validating it.

#### MR1-P02 — Insert version-dispatch without byte drift

Subject:

```text
refactor(revision): insert version-dispatch codec seam
```

Exact allowlist:

```text
M docs/orchestrated/vibecad-multi-runtime-g1.md
A src/vibecad/execution/revision_codec.py
M src/vibecad/execution/revisions.py
A tests/test_revision_codec.py
M tests/test_revision_store.py
M tests/test_durable_v1_corpus.py
```

The codec accepts and returns immutable decoded values without store or path
authority. Dispatch is strict over schema version, exact keyset and checksum
domain. The writer remains hard-pinned to v1. `RevisionRef.to_mapping()` and
its digest-bound projections do not gain a profile field. Unknown versions,
domains, fields, duplicate JSON keys and v1/v2 hybrids fail closed.

#### MR1-P03 — Add read-only inventory and preflight

Subject:

```text
feat(migration): add read-only durable inventory
```

Exact allowlist:

```text
M docs/orchestrated/vibecad-multi-runtime-g1.md
A src/vibecad/application/durable_migration.py
M src/vibecad/application/data.py
M src/vibecad/application/project.py
M src/vibecad/application/project_create.py
M src/vibecad/application/artifacts.py
M src/vibecad/execution/revisions.py
M src/vibecad/interaction/checkouts.py
A tests/test_durable_migration.py
M tests/test_revision_store.py
M tests/test_task_store.py
M tests/test_managed_checkout.py
M tests/test_artifact_materialization.py
M tests/test_project_api.py
M tests/test_project_bootstrap.py
```

Each modification outside the new migration module is limited to a bounded,
read-only snapshot hook. `workflow/store.py` is excluded because it already
has a Task snapshot; a proven missing read-only fact requires a separately
approved commit.

Inventory covers `projects/`, `tasks/`, `bootstrap/`, `checkouts/` and
`artifacts/`, including bootstrap request/staging/work/normalized residue.
It is path-free, bounded, deterministic and mutation-negative. `ready=true`
is not a P03 output: a sequential read-only scan cannot prove quiescence
against a concurrent writer. P03 reports `structurally_ready=true` only when
the observed snapshot has zero unknown/corrupt/ambiguous/dangling entries, no
observed active candidate/journal/reservation/temp state and exact
reference/digest closure; it also reports a closed set of observational
blockers and the start/end change tokens used to detect visible drift.

P03 cannot start a Worker/runtime, acquire a project write lease, repair,
reconcile, delete or expose an absolute path. A later writer-activation stage
must quiesce the daemon and hold an approved global writer/maintenance fence,
then rerun the complete scan. Only that fenced second scan may report
`activation_ready=true`; its lock design and exact source allowlist require a
future approval.

MR1-P04 and later v2 reader/writer activation require a new append with exact
allowlists after P00..P03 are GREEN. Durable v2 is not equivalent to a
repo-wide multi-CAD public/domain rewrite: until real second-CAD demand, the
reader may normalize legacy and v2 FreeCAD records to the current compatibility
projection while the migration layer retains profile metadata.

### 28.6 Track-local and mechanical gates

The initial post-A04 commit budget is fixed:

| Campaign | Normal commits | Conditional commits | Stage limit |
|---|---:|---:|---|
| G1 | 7 (`C00P`, `C00B`, `C00`..`C04`) | 0; C00B has one approved mutually exclusive diff branch | one current commit's exact allowlist |
| MR1-prep | 4 (`P00`..`P03`) | 0 | one current commit's exact allowlist |

This artifact-only R2 recovery commit is outside the source budget. No
integration, packaging, release, MR1-P04 or later commit is authorized by this
budget. The index must be empty before each stage, may contain only one
commit's named allowlist, and must be emptied by that commit before another
track stages anything. A second corrective commit, a new path or a change in
commit semantics stops the campaign for an append and approval.

Before every semantic commit, the controller appends that attempt's candidate
hashes, RED/GREEN/gate evidence, residuals and the preceding commit/push fact
to this artifact, then stages it with the current exact source allowlist. A
commit cannot contain its own final hash or post-push fact. The next semantic
commit's preamble binds those facts. If a track blocks, hands off or reaches
its last authorized commit without a successor, one mandatory
controller-only evidence/recovery commit may append the missing hash, push
state and recovery snapshot using this exact one-path allowlist:

```text
M docs/orchestrated/vibecad-multi-runtime-g1.md
```

Such artifact-only recovery commits are required process records, not source
budget or corrective commits; they cannot change a decision, source allowlist
or product claim.

Expected impact through the authorized budget:

- public surface remains exactly 28 tools and six operations;
- Task/Revision/Accept/Reject and `SelectorV1` wire contracts remain unchanged;
- MR1-prep writes only v1 bytes and must prove them byte-identical;
- G1 adds a repository addon and isolated real-GUI test surface, but does not
  publish or install it into a user's normal FreeCAD tree;
- existing test cases are not deleted, combined or weakened; focused counts
  may only increase as new tests are added.

The named real-GUI/manual matrix is:

| Gate | Scenario | Required evidence | Owner / closure |
|---|---|---|---|
| G1-V00 | isolated managed FreeCAD discovers one addon and activates Dock | bounded harness result, process/thread identities, Qt heartbeat, clean GUI exit, exact daemon retirement and pid/socket/run-root absence | controller; required before C01 commit |
| G1-V01 | choose project/task and open separate real HEAD/draft FCStd Preview Documents | screenshot plus checkout/grant/document identity log; no real user data root | controller; required before C02 commit |
| G1-V02 | dirty, stale and revoked transitions | visible disabled Accept; discard/reopen recovery; Reject leaves HEAD digest unchanged | controller; required before C03 commit |
| G1-V03 | Accept and Reject across daemon/FreeCAD restart | accepted HEAD advances exactly once; rejected HEAD unchanged; Task/verdict rediscovered | controller; required before G1 alpha claim |
| G1-V04 | whole managed object and feature-entity capture | copied SelectorV1 round-trips uniquely to the same object; Face/Edge visibly unsupported | controller; required before C04 commit |
| G1-M01 | complete in-FreeCAD product flow at normal display scale | user-visible Dock, preview, verdict, Accept/Reject and selection review | user useful but not required for mechanical commits; required to close RES-03 product acceptance |

The exact independent focused-command matrix is:

| Gate | Commit | Exact command |
|---|---|---|
| G1-G00P | C00P | `.venv/bin/python -m pytest -q tests/test_paths.py && .venv/bin/python -m ruff check src/vibecad/runtime/paths.py tests/test_paths.py` |
| G1-G00B | C00B | `.venv/bin/python -m pytest -q tests/test_freecad_workbench_bootstrap.py && .venv/bin/python -m ruff check src/vibecad/daemon/bootstrap.py tests/test_p0b_acceptance.py tests/fixtures/freecad_workbench/bootstrap_probe.py tests/test_freecad_workbench_bootstrap.py` |
| G1-G00B-R | C00B correction branch only | `.venv/bin/python -m pytest -q tests/test_p0b_acceptance.py::test_embedded_freecad_uses_managed_python_for_cold_daemon` |
| G1-G00B-F | C00B real probe | `VIBECAD_RUN_INTEGRATION=1 VIBECAD_FREECAD_ENV=<verified-prefix> .venv/bin/python -m pytest -q -m slow tests/test_freecad_workbench_bootstrap.py` |
| G1-G00 | C00 | `.venv/bin/python -m pytest -q tests/test_freecad_workbench_package.py tests/test_freecad_workbench_controller.py && .venv/bin/python -m ruff check freecad/VibeCAD/Init.py freecad/VibeCAD/InitGui.py freecad/VibeCAD/vibecad_workbench tests/fixtures/freecad_workbench/fake_host.py tests/test_freecad_workbench_package.py tests/test_freecad_workbench_controller.py` |
| G1-G01 | C01 | `.venv/bin/python -m pytest -q tests/test_freecad_workbench_package.py tests/test_freecad_workbench_controller.py tests/test_freecad_workbench_gui.py && .venv/bin/python -m ruff check freecad/VibeCAD tests/fixtures/freecad_workbench/fake_host.py tests/fixtures/freecad_workbench/gui_harness.py tests/test_freecad_workbench_controller.py tests/test_freecad_workbench_gui.py` |
| G1-G01-F | C01 real GUI / M00 | `VIBECAD_RUN_INTEGRATION=1 VIBECAD_FREECAD_ENV=<verified-prefix> .venv/bin/python -m pytest -q -m slow tests/test_freecad_workbench_gui.py` |
| G1-G02 | C02 | `.venv/bin/python -m pytest -q tests/test_freecad_workbench_controller.py tests/test_freecad_workbench_preview.py && .venv/bin/python -m ruff check freecad/VibeCAD/vibecad_workbench tests/fixtures/freecad_workbench/fake_host.py tests/test_freecad_workbench_controller.py tests/test_freecad_workbench_preview.py` |
| G1-G03 | C03 | `.venv/bin/python -m pytest -q tests/test_freecad_workbench_controller.py tests/test_freecad_workbench_preview.py tests/test_freecad_workbench_review.py && .venv/bin/python -m ruff check freecad/VibeCAD/vibecad_workbench tests/fixtures/freecad_workbench/fake_host.py tests/test_freecad_workbench_controller.py tests/test_freecad_workbench_preview.py tests/test_freecad_workbench_review.py` |
| G1-G04 | C04 | `.venv/bin/python -m pytest -q tests/test_freecad_workbench_controller.py tests/test_freecad_workbench_selection.py tests/test_object_selectors.py && .venv/bin/python -m ruff check freecad/VibeCAD/vibecad_workbench tests/fixtures/freecad_workbench/fake_host.py tests/test_freecad_workbench_controller.py tests/test_freecad_workbench_selection.py` |
| MR1-G00 | P00 | `git diff --check && .venv/bin/python -m pytest -q tests/test_agent_skill.py` |
| MR1-G01 | P01 | `.venv/bin/python -m pytest -q tests/test_durable_v1_corpus.py && .venv/bin/python -m ruff check tests/test_durable_v1_corpus.py` |
| MR1-G02 | P02 | `.venv/bin/python -m pytest -q tests/test_durable_v1_corpus.py tests/test_revision_codec.py tests/test_revision_store.py && .venv/bin/python -m ruff check src/vibecad/execution/revision_codec.py src/vibecad/execution/revisions.py tests/test_durable_v1_corpus.py tests/test_revision_codec.py tests/test_revision_store.py` |
| MR1-G03 | P03 | `.venv/bin/python -m pytest -q tests/test_durable_migration.py tests/test_revision_store.py tests/test_task_store.py tests/test_managed_checkout.py tests/test_artifact_materialization.py tests/test_project_api.py tests/test_project_bootstrap.py && .venv/bin/python -m ruff check src/vibecad/application/durable_migration.py src/vibecad/application/data.py src/vibecad/application/project.py src/vibecad/application/project_create.py src/vibecad/application/artifacts.py src/vibecad/execution/revisions.py src/vibecad/interaction/checkouts.py tests/test_durable_migration.py tests/test_revision_store.py tests/test_task_store.py tests/test_managed_checkout.py tests/test_artifact_materialization.py tests/test_project_api.py tests/test_project_bootstrap.py` |

For every behavior-change row, RED is the first expected focused failure on
absent or incorrect behavior; GREEN is the exact command above returning zero
on the candidate bytes. P00 uses its explicit documentation gap audit instead
of a fabricated test RED. C00B may be direct GREEN only on its
characterization branch; its correction branch must preserve the reproduced
real RED. The real commands replace `<verified-prefix>` with the
identity-bound absolute prefix recorded by M00. Each row is followed by the
same independent named-allowlist/status/index/hash/`git diff --check`
mechanical gate before staging.

`G1-G00B-F` and `G1-G01-F` fail unless their parent wrapper retires the exact
recorded isolated daemon within the bound and verifies pid/socket/process/live
run-root absence after the GUI child exits.

The track gates are:

- every source commit: focused RED, focused GREEN, affected tests, Ruff,
  allowlist/status/index/hash/diff checks and a `gpt-5.6-sol / max`
  adversarial review before commit;
- pure mechanical gates: independent `gpt-5.6-terra / medium` subagent;
- ordinary implementation: `gpt-5.6-sol / high` subagent;
- critical architecture, durable migration and adversarial review:
  `gpt-5.6-sol / max` subagent;
- G1: package/import tests plus real FreeCAD GUI evidence; fake Qt or
  `freecadcmd` cannot close G1 visual/thread gates;
- MR1: v1 corpus byte equivalence, existing crash/reconcile suites and a
  preflight before/after snapshot proving an identical path set, content
  hashes, file kind, mode, uid, size, mtime, ctime and object identity. Access
  time is excluded because the verification read itself may change atime.

No broad `git add`, generated update of golden bytes, retry-until-green,
test-count reduction, waiver or unrecorded correction is authorized.

### 28.7 Shared non-disposable beta gate

G1 alpha and MR1 development join before the first promised non-disposable
external project. The exact integrated build must prove:

1. old v1 projects remain byte-identical and readable;
2. new v2 revisions and mixed v1 -> v2 ancestry can create, review, revert and
   reconcile;
3. a pending draft survives process and FreeCAD restart;
4. G1 opens v1 and mixed-lineage HEAD/draft previews through opaque checkout
   and grant contracts;
5. verdict, stale/revoked/dirty state and Accept/Reject remain authoritative;
6. `vibecad://artifact/{materialization_id}/{artifact_id}` stays compatible;
7. interrupted activation/migration, backup restore and rollback converge;
8. an old writer encounters v2 state and fails closed;
9. source files and rejected HEAD remain unpolluted;
10. addon, wheel, MCPB and host skill bytes have exact reviewed hashes.

Any changed byte in immutable v1 history, data loss, ambiguous recovery,
unknown preflight entry, insufficient free space/capacity, backup not restored
and independently verified, dry run exceeding the declared maintenance window,
G1 durable-path dependency or old-writer mutation is a release breaker. A real
snapshot dry run is forced by any of 100 projects, 1,000 revisions or 10 GiB
of v1 data.

### 28.8 Dynamic user-owned exclusions

After MR0 C05 was committed and pushed, these untracked paths appeared:

```text
CAD_Theory_Course_Scripts_V4.md
CAD_Theory_Course_Scripts_V5.md
CAD_Theory_Course_Scripts_V6_Expanded.md
CAD_Theory_Course_Scripts_V7_FullExpanded.md
CAD_Theory_Course_Scripts_V8_True3000.md
```

They are user-owned, outside all G1/MR1 allowlists and are demonstrably
changing: V7 and V8 appeared during the first R2 mechanical gate. The
controller and subagents must not read, edit, delete, package, stage or commit
them. Every stage and gate must re-enumerate the complete untracked-name set
and use exact named staging; later matching course-script paths receive the
same exclusion even before this ledger is updated. The anchored name-only
exclusion is `\ACAD_Theory_Course_Scripts_[^/]*\.md\Z` and applies only to
untracked root paths; it never makes a tracked path ignorable. Their presence
means the worktree is not globally clean; only the tracked tree may be
described as clean.

### 28.9 Recovery Snapshot MRG1-S08

Snapshot time: `2026-07-26T08:09:42Z`.

#### S08-1 — Completed milestones

- MR0 C00..C05 is complete at
  `6cc1876 -> 07c6d6 -> 6c3581b -> 71a25b5 -> 7c98e36 -> 2de1a37`;
  final C05 gates and push evidence are recorded in Sections 27.12–27.15 and
  Section 28.1; artifact revision before this append is `MRG1-R1`.
- branch is `codex/agent-stage3`; HEAD and upstream are both
  `2de1a37cc7e67268965a5a7b9519b2bf0e049f9a`.
- Three sol/max source audits completed for G1, MR1 and their integration
  boundary. The first R2 terra/medium mechanical gate proved a strict artifact
  append. The R2 adversarial findings were closed with
  `0 blocker / 0 critical / 0 major / 0 minor`; the final terra/medium
  pre-stage gate remains next, so no final mechanical GREEN is claimed yet.
- Before this append the tracked index/worktree was clean. Current tracked diff
  is this artifact only; no source, test, package or durable-data byte changed.
  The five observed course scripts and any later anchored match from
  Section 28.8 remain untracked user-owned exclusions.

#### S08-2 — Ordered next packets and branch conditions

1. Close every R2 adversarial finding; rerun sol/max adversarial and
   terra/medium mechanical gates on the final bytes.
2. If both are GREEN, stage only this artifact, verify cached strict-append and
   status identity, create one artifact-only R2 recovery commit and push it.
3. Show the corrected packet and wait for MRG1-A04. No source packet starts
   while A04 is absent.
4. If the user grants A04, first append the exact words, timestamp, artifact
   revision, decisions, budget, allowlists and gates to this artifact; run the
   artifact G0; create and push a second artifact-only authorization commit.
   That authorization commit is outside the source budget.
5. Only after the A04 commit is upstream, start G1-C00P and MR1-P00 in disjoint
   domains. Then run G1-C00B:
   - if the real embedded bootstrap probe is GREEN, commit the controller
     artifact plus only its two new test paths;
   - if it reproduces the specified `sys.executable` failure, correct only the
     two conditional named paths in that same RED/GREEN commit;
   - any other failure stops G1.
6. Continue G1-C00..C04 and MR1-P01..P03 in order. Stop before packaging,
   integrated real Accept/Reject, MR1-P04 or v2 activation for a new exact
   append and approval.

#### S08-3 — Active approved decisions and authorization

- MRG1-A01 authorizes the completed MR0 decisions D01..D16; MRG1-A02 controls
  subagent model/reasoning routing.
- MRG1-A03 approves only: second CAD deferred, FreeCAD end-to-end G1 first,
  MR1 in parallel and a common gate before non-disposable external beta.
- MRG1-A04 is reserved and absent. Sections 28.2–28.8 are proposed, not
  executable.
- Workbench remains a thin client of one Task Kernel; FreeCAD is the only
  connected CAD adapter; durable storage remains fixed v1 FCStd/STEP through
  MR1-prep.
- The exclusions in Sections 28.1, 28.3, 28.7 and 28.8 are active. A new
  public schema/tool, second CAD, face/edge, manual publish, Addon Manager,
  release, external credential/spend, unnamed path or shared-file collision
  requires a new decision and approval.

#### S08-4 — Execution discipline

Required capability profile:

```text
approval: artifact-approval
delegation: spawn-send-wait
persistence: repo-artifact
process: native-session-poll
```

Required adapter-selection evidence:

- `live capability declarations`: the current Codex desktop session declares
  commentary/final user channels, local patch/command tools, direct
  `spawn_agent`/`send_message`/`wait_agent` delegation and resumable
  `exec_command`/`write_stdin` sessions. It declares Default rather than native
  Plan approval mode. This supports artifact approval, spawn-send-wait,
  repo-artifact writes and native session polling.
- `observable behavior`: this campaign already spawned, messaged and waited for
  independent agents; MR0 persisted and pushed this repo artifact; one bounded
  FreeCAD command returned a live session id and was polled and terminated
  through that same session. This supports the four selected profile values.
- `environment identity`: the passive host context identifies Codex desktop,
  root controller `/root`, a four-slot multi-agent tree and workspace
  `/Users/wangtao/Documents/DevProject/vibecad`. This selects the Codex desktop
  repo-artifact controller adapter.
- `public configuration`: current public session configuration exposes Default
  collaboration mode, unrestricted workspace filesystem, no command-approval
  prompt and the available sol/terra model overrides. It exposes no native
  Plan approval control in this mode.

Selected adapter: Codex desktop repo-artifact controller with
`spawn-send-wait` task packets, exact named local staging, immediate
commit/push and `native-session-poll` for each long process. Adapter tier
mapping is `fast -> gpt-5.6-terra / medium` for mechanical gates,
`standard -> gpt-5.6-sol / high` for ordinary coding and
`deep -> gpt-5.6-sol / max` for durable architecture and adversarial review.

Allowlist and stage discipline are Sections 28.3–28.6. Gates include per-row
RED/GREEN commands, Ruff, real GUI proof, sol/max review, terra/medium
mechanical verification and shared beta gates. Circuit breakers are unnamed or
shared writes, unapproved correction, v1 byte drift, observational preflight
claimed as activation-ready, fake GUI evidence, dirty/stale Accept, ambiguous
selector/recovery, data/capacity/backup failure and dynamic user-file contact.
Residuals stay OPEN until their named closure gate; no retry, waiver, golden
rewrite or test weakening closes one. Recovery always re-reads the full
artifact, verifies branch/HEAD/upstream/status/approval/capability profile,
preserves user exclusions, checks the last commit's gates and resumes only the
first unclosed ordered packet.

Recovery procedure:

1. Read this entire artifact and recover MRG1-S08 without chat memory.
2. Verify branch, HEAD/upstream and the complete status before any write.
3. Preserve every untracked path exactly and prohibit broad staging.
4. If the R2 append is uncommitted, verify it is a strict append to
   `2de1a37` and finish only the artifact recovery commit.
5. If the R2 recovery commit exists but A04 is absent, show the packet and wait;
   do not start source implementation.
6. If A04 exists, start or resume G1 and MR1 only at the first unclosed exact
   commit above.
7. Stop on any write-domain collision, unnamed path, durable mutation during
   MR1-prep, non-real G1 GUI proof or new product decision.

| Entry ID | Decision / approval | Commit / push | Gate evidence | Residual | Snapshot | State |
|---|---|---|---|---|---|---|
| MRG1-R2-E01 | MRG1-A03 direction; A01; A02; A04 reserved | `not-created`; artifact-only next | three sol/max source audits; R2 adversarial found 2 blockers / 6 major / 3 minor and correction is in progress | RES-01A/03/04/05/06A/07/08/09/10/10A; G1 GUI bootstrap and MR1-prep open | MRG1-S08 | direction approved / exact implementation pending / corrected R2 gate next |

### 28.10 MRG1-R2 adversarial correction and GO

The first deep review correctly rejected R2's attempt to bind MRG1-A03
retroactively to an unseen exact packet and rejected an observational MR1 scan
claiming quiescent activation readiness. Successive read-only reviews then
found and closed:

- reproducible real-GUI harness, isolated VibeCAD/FreeCAD roots and repository
  source-identity gaps;
- dirty-preview, selector uniqueness and daemon-retirement gaps;
- exact commit budget, command matrix, manual validation and beta-breaker
  gaps;
- C00B stage-order and embedded `sys.executable` observability deadlocks;
- checkout v1/v2 and project-create golden-corpus omissions;
- the required capability profile, adapter evidence categories and four-part
  recovery snapshot;
- per-commit rolling-ledger allowlist and self-hash/push recording discipline;
- invalid test-only daemon retirement reason and C00B scope wording.

On the latest `865`-line strict append, the independent
`gpt-5.6-sol / max` reviewer returned:

```text
blocker:  0
critical: 0
major:    0
minor:    0
decision: GO to terra/medium mechanical gate
git diff --check: PASS
course-script contents touched: 0
```

No G1/MR1 source, test, package or durable byte has been written. MRG1-A04 is
still absent. The first two terra/medium checks on earlier R2 candidates
already proved strict append, exact one-file tracked diff, balanced fences,
zero broken relative links and correct dynamic user exclusions; those are
intermediate evidence only. A fresh full mechanical gate on the exact current
bytes is required before staging.

| Entry ID | Decision / approval | Commit / push | Gate evidence | Residual | Snapshot | State |
|---|---|---|---|---|---|---|
| MRG1-R2-E02 | A03 direction; D17/D18 active; D19..D22 proposed; A04 absent | `not-created`; forbidden before final mechanical gate | sol/max 0/0/0/0 GO; strict append diff-check PASS; no user-file content contact | exact implementation not approved; final terra pre-stage pending | MRG1-S08 | adversarial GREEN / mechanical gate next |

### 28.11 MRG1-R2 final pre-stage mechanical PASS

An independent `gpt-5.6-terra / medium` subagent ran the complete read-only
mechanical gate on the exact Section 28.10 candidate. It did not run pytest or
FreeCAD and did not read, edit, stage or package any course-script content.
Every check exited zero.

```text
branch:              codex/agent-stage3
HEAD/upstream:       2de1a37cc7e67268965a5a7b9519b2bf0e049f9a / same
ahead/behind:        0 / 0
tracked diff:        artifact only
index/cached diff:   empty
untracked excluded:  V4, V5, V6_Expanded, V7_FullExpanded, V8_True3000

HEAD artifact:       192508 bytes
HEAD SHA-256:        179dd6d8248fdaec5aeb8cd30addb506351f8a863fe3a18b5d2b5558e7678223
candidate artifact:  238555 bytes
candidate SHA-256:   1dac56a398bd68bfae067a397330b34152978bc5bcd7a1d1b18cefbcd3464cf6
strict byte append:  PASS
git diff --check:    PASS

relative links:      0 / 0 broken
Section 28 fences:   56 / balanced
basic table issues:  0
semantic packets:    11
allowlist entries:   111 / 0 invalid
command mappings:    11 / 0 missing
source/test/package/durable changes: 0
```

The gate also proved:

- all five current dynamic root scripts match the anchored untracked-only
  exclusion;
- all 11 semantic packets include the controller artifact and agree with the
  `7 + 4` source budget;
- A03 is direction-only, A04 is reserved/absent and both text and actual diff
  prohibit source implementation;
- S08-1..S08-4, the four capability values and the four exact permitted
  evidence-source category names are present;
- no broad staging command exists in R2.

This evidence append changes only the controller artifact. Exact named staging
of that one path is now authorized. A cached-only terra/medium gate must then
recheck the staged path set, cached/worktree byte identity, strict HEAD prefix,
diff-check, dynamic exclusions, A04 absence and zero source diff. No expensive
or real gate is rerun.

| Entry ID | Decision / approval | Commit / push | Gate evidence | Residual | Snapshot | State |
|---|---|---|---|---|---|---|
| MRG1-R2-E03 | A03 direction; A04 absent; artifact persistence only | `not-created`; exact artifact staging next | sol/max 0/0/0/0 GO; terra pre-stage PASS; 11 packets / 111 entries; strict append and dynamic exclusions PASS | source implementation still forbidden pending A04 | MRG1-S08 | pre-stage GREEN / cached-only gate next |

## 29. MRG1-A04 Authorization and Recovery Snapshot MRG1-S09

### 29.1 MRG1-R2 persisted recovery evidence

The Section 28 artifact-only recovery candidate passed its final independent
cached-only `gpt-5.6-terra / medium` gate:

```text
cached paths:         1 / docs/orchestrated/vibecad-multi-runtime-g1.md
unstaged tracked:     0
HEAD artifact:        192508 bytes
cached/worktree:      240952 bytes / byte-identical
cached/worktree hash: e80ac502f7c468205403ee85be57dae5a9a988ac3d20267b400f10cb5e494023
strict HEAD append:   PASS for cached and worktree
cached/worktree diff: PASS
relative links:       0 / 0 broken
Section 28 fences:    58 / balanced
basic table issues:   0
source diff:          0
dynamic exclusions:  exact V4, V5, V6_Expanded, V7_FullExpanded, V8_True3000
```

The recovery commit was then created and pushed:

```text
commit: 8b220d4d1f4c3d8bf704ad70e41b88c2096d63f3
subject: docs(orchestration): prepare G1 and MR1 parallel execution
push: origin/codex/agent-stage3
post-push HEAD/upstream: equal
ahead/behind: 0 / 0
```

Only the rolling artifact was committed. All five course scripts remained
untracked and untouched.

### 29.2 Exact authorization MRG1-A04

After R2 was committed, pushed and shown, the user explicitly granted the
reserved exact implementation approval:

| Approval | Timestamp | Approved artifact anchor | Exact scope | Exact user text | State |
|---|---|---|---|---|---|
| MRG1-A04 | 2026-07-26T10:12:40Z | MRG1-R2 at `8b220d4d1f4c3d8bf704ad70e41b88c2096d63f3` | Sections 28.2–28.8; D19..D22; G1 `C00P`, `C00B`, `C00`..`C04`; MR1 `P00`..`P03`; command/manual gates; conditional C00B branch; shared non-disposable beta gate | `批准 MRG1-A04` | approved; executable only after this authorization record is committed and pushed |

MRG1-A04 activates D19..D22 and the exact initial `7 + 4` semantic source
budget. It inherits A01/A02/A03, the higher-priority instructions, the current
host permission model and sandbox, every exact allowlist, gate, circuit breaker
and exclusion in R2. Neither this artifact, the Skill nor any task packet can
grant or expand permissions, elevate authority or bypass that model or
sandbox.

The approval does not authorize packaging/real Accept-Reject integration
beyond the first tranche, MR1-P04/v2 writer activation, a second CAD,
face/edge, semantic diff, manual publish, Addon Manager, release/tag, external
credentials or spend. It does not authorize any contact with the dynamic
course scripts.

### 29.3 Authorization persistence and first execution boundary

This Section 29 append is the required artifact-only authorization record. Its
exact one-path allowlist is:

```text
M docs/orchestrated/vibecad-multi-runtime-g1.md
```

It must pass a deep approval-binding review and a fast mechanical gate, be
committed with subject:

```text
docs(orchestration): authorize G1 and MR1 execution
```

and reach `origin/codex/agent-stage3` before any source packet starts. The
commit's self-hash and push fact will be recorded in the first semantic
commit's controller ledger preamble.

Once upstream, two independent first packets may be developed concurrently:

- G1-C00P, `standard -> gpt-5.6-sol / high`, owns only
  `src/vibecad/runtime/paths.py` and `tests/test_paths.py`;
- MR1-P00, `deep -> gpt-5.6-sol / max`, owns only
  `docs/ARCHITECTURE.md`, `docs/ACCEPTANCE_TESTS.md`,
  `docs/CAD_RUNTIME_ADAPTER_GUIDE.md` and new
  `docs/orchestrated/vibecad-durable-v2.md`.

Implementation subagents must not edit, stage, commit or push the shared
rolling artifact. They return candidate bytes and evidence. The controller
serially appends the ledger, reviews, stages one exact semantic commit,
commits and pushes before staging the other track.

### 29.4 Recovery Snapshot MRG1-S09

#### S09-1 — Completed milestones

- MR0 C00..C05 is complete and pushed through `2de1a37`.
- MRG1-R2 passed sol/max `0/0/0/0`, terra pre-stage and cached-only gates, then
  committed and pushed at `8b220d4`.
- The user explicitly granted MRG1-A04 against that pushed R2 anchor.
- At authorization receipt, tracked index/worktree were clean; only the five
  anchored dynamic user files were untracked.

#### S09-2 — Ordered next packets and branch conditions

1. Deep-review and mechanically gate this exact Section 29 append.
   - if either gate is RED, stop without staging, append a blocked S09
     successor with the exact evidence and keep source forbidden;
   - if the dynamic exclusion set changes or an unmatched user-owned path
     appears, stop, enumerate names only, preserve every file and update the
     exclusion/recovery record before retrying the gate.
2. Stage only the artifact, commit and push the authorization record.
   - if staged paths or cached/worktree bytes differ, unstage nothing
     destructively; stop and inspect the index against the one-path allowlist;
   - if commit or push fails, retain the observable state, append a blocked
     recovery entry when safely possible and do not start source.
3. Verify HEAD/upstream equality and the complete dynamic exclusion set.
   - if the post-push hashes/ahead-behind state mismatch, stop and recover from
     the pushed repository state;
   - if the host, tools or any capability-profile field changes, repeat passive
     discovery using only the four permitted evidence categories, append a new
     profile/snapshot and keep source paused until it is gated.
4. Spawn G1-C00P at standard tier and MR1-P00 at deep tier with complete
   seven-section packets anchored to the authorization commit.
5. If either candidate touches an unnamed/shared path, changes its semantic
   scope or waits for the already-satisfied approval, stop that packet and
   route the observable failure through the controller.
   If a long process becomes ambiguous, poll only its original native session;
   never duplicate launch or infer success. An unresolved process identity is
   a blocked recovery condition.
6. Review candidates independently. The controller selects one, appends its
   ledger evidence, stages only that commit's allowlist, gates, commits and
   pushes; then repeats for the other.

#### S09-3 — Active decisions and authorization

- A01/A02 remain active; A03 approves the parallel product direction.
- A04 now approves Sections 28.2–28.8 and activates D19..D22, subject to this
  authorization record reaching upstream.
- D17/D18 remain active. Second CAD stays deferred; FreeCAD G1 is the product
  priority; MR1-prep stays immutable-v1 and observational only.
- The Section 28.7 beta breakers and Section 28.8 user-file exclusions remain
  active without waiver.

#### S09-4 — Execution discipline

Capability profile remains:

```text
approval: artifact-approval
delegation: spawn-send-wait
persistence: repo-artifact
process: native-session-poll
```

Required adapter-selection evidence for this recorded profile:

- `live capability declarations`: the current Codex desktop session still
  declares user commentary/final channels, local patch/command tools,
  `spawn_agent`/`send_message`/`wait_agent`, and resumable
  `exec_command`/`write_stdin` sessions. These declarations support
  artifact-approval, spawn-send-wait, repo-artifact and native-session-poll.
- `observable behavior`: in this live campaign the controller has persisted
  and pushed R2, spawned/sent/waited for deep and mechanical agents, and polled
  the original id of a bounded process. These already-observed behaviors
  support all four selected fields.
- `environment identity`: passive host context still identifies Codex desktop,
  root controller `/root`, the four-slot agent tree and repository workspace
  `/Users/wangtao/Documents/DevProject/vibecad`.
- `public configuration`: current public session configuration still exposes
  Default collaboration mode, unrestricted workspace filesystem, no command
  approval prompt and the sol/terra model overrides; native Plan approval is
  not exposed in this mode.

Selected adapter remains the Codex desktop repo-artifact controller.
`standard` maps to sol/high, `deep` to sol/max and `fast` to terra/medium.
There is no applicable repository-root or directory-scoped `AGENTS.md` or
`CLAUDE.md`. Every packet inherits higher-priority system/developer/user
instructions, its exact R2 allowlist and the current host permission model and
sandbox. Long processes use their original native session id; no duplicate
launch or marker fallback is allowed. Residuals, gate REDs and blockers remain
open until observable forward evidence closes them.

Exact source allowlists and controller-only rolling-ledger ownership are
Sections 28.3–28.5. The stage limit is the approved `7 + 4` semantic budget,
one current commit allowlist and one controller-owned index at a time. Exact
focused/real/manual/adversarial/mechanical gates are Section 28.6; the shared
beta gate is Section 28.7. Circuit breakers include any unnamed/shared path,
unexpected or repeated gate RED, process/session ambiguity, source before the
A04 push, stage/index drift, failed commit/push or post-push mismatch,
capability/profile drift, dynamic user-file contact, v1 byte drift,
observational readiness presented as activation readiness, fake GUI evidence,
dirty/stale Accept, ambiguous selector/recovery, or data/capacity/backup
failure. No waiver, retry-until-green, test weakening or golden rewrite closes
one.

Recovery reads the full artifact, verifies the last pushed hash,
HEAD/upstream/status/index, A04 record, capability profile and its four
evidence categories, dynamic exclusions, prior gate evidence and the first
unclosed packet. Residuals remain OPEN until their named observable closure
gate. Recovery never uses chat memory alone.

| Entry ID | Decision / approval | Commit / push | Gate evidence | Residual | Snapshot | State |
|---|---|---|---|---|---|---|
| MRG1-A04-E01 | A01..A04; D01..D22 within their exact stages | `not-created`; artifact-only next | R2 push verified; A04 exact user text/time/anchor recorded; source diff 0; authorization deep/mechanical gates in progress | none within authorization append; inherited RES-01A/03/04/05/06A/07/08/09/10/10A remain OPEN under their named closure gates | MRG1-S09 | approved / persistence gate next / source still paused |

### 29.5 MRG1-A04 pre-stage gate PASS

The exact authorization append passed independent review on its final
pre-evidence bytes.

Deep `gpt-5.6-sol / max` result:

```text
blocker:  0
critical: 0
major:    0
minor:    0
decision: GO
```

Fast `gpt-5.6-terra / medium` result:

```text
branch/HEAD/upstream: codex/agent-stage3 / 8b220d4... / same
ahead/behind:         0 / 0
tracked diff:         artifact only
cached/index:         empty
source diff:          0
dynamic exclusions:  exact five / all anchored matches

HEAD artifact:        240952 bytes
HEAD SHA-256:         e80ac502f7c468205403ee85be57dae5a9a988ac3d20267b400f10cb5e494023
candidate artifact:   251209 bytes
candidate SHA-256:    a230fd23563eab6e7327dd5d6d90fce005da56fd8f116d28f1c887cfc60c930b
strict byte append:   PASS
git diff --check:     PASS
table schema:         PASS
```

The reviewers confirmed the exact A04 text/time/anchor/scope, non-expansion,
authorization-commit-before-source condition, first-packet write isolation,
S09 four-section recovery snapshot, capability profile/evidence categories,
all observable recovery branches, stage/gate/breaker discipline and residual
schema. No course-script content was read or touched.

This evidence append remains artifact-only. Exact named staging of this
artifact is authorized, followed by one cached-only mechanical gate. Source
implementation remains paused until the resulting authorization commit is
pushed and HEAD/upstream equality is verified.

| Entry ID | Decision / approval | Commit / push | Gate evidence | Residual | Snapshot | State |
|---|---|---|---|---|---|---|
| MRG1-A04-E02 | A01..A04; D17..D22 active within R2 | `not-created`; exact artifact stage next | deep 0/0/0/0 GO; fast pre-stage PASS; exact A04 binding and source diff 0 | inherited RES-01A/03/04/05/06A/07/08/09/10/10A remain OPEN | MRG1-S09 | pre-stage GREEN / cached-only gate next / source paused |

## 30. MRG1-GATE-CORR-01 and G1-C00P Candidate Ledger

### 30.1 Authorization persistence completed

The Section 29 authorization record passed its exact cached-only gate, was
committed and pushed before either implementation packet wrote candidate
bytes:

```text
commit: 4d92d04eff11213a9c539c316451427a51f4dc6b
subject: docs(orchestration): authorize G1 and MR1 execution
push: origin/codex/agent-stage3
post-push HEAD/upstream: equal
ahead/behind: 0 / 0
```

This closes the source-pause condition in Section 29.3. MRG1-A04 is active
only within its exact Sections 28.2–28.8 scope and exclusions.

### 30.2 MRG1-GATE-CORR-01

The first raw G1-G00P pytest invocation:

```text
.venv/bin/python -m pytest -q tests/test_paths.py \
  -k freecad_path_honors_override_without_side_effects
```

stopped during collection with exit 2 and:

```text
ModuleNotFoundError: No module named 'vibecad'
```

Passive diagnosis established that `.venv/bin/python` is Python 3.13.14,
`find_spec("vibecad")` is absent, the environment has no editable-project
path, and the repository's documented source-checkout invocation prepends
`PYTHONPATH=src`. This is the same inherited checkout condition already
recorded by `P0B-GATE-CORR-01`; it is a setup breaker, not semantic RED
evidence.

An independent `gpt-5.6-sol / max` ruling returned GO for the following exact,
non-semantic correction:

- prepend `PYTHONPATH=src` to every Python pytest invocation in the Section
  28.6 command matrix;
- leave Ruff, Git, manual and real-host environment fields otherwise
  unchanged;
- do not install or modify the environment, `.venv`, project configuration,
  source paths, tests or acceptance criteria;
- require the corrected command to expose a genuine focused RED before a
  behavior-changing production edit;
- carry this correction in the current semantic ledger rather than create a
  separate correction commit.

Any correction that needs an install, environment mutation, `pyproject`
change, new path, weakened test or altered product claim remains a breaker and
requires a new packet. Every later mechanical agent must verify the corrected
pytest projection explicitly.

With only the new test present, the corrected focused command:

```text
PYTHONPATH=src .venv/bin/python -m pytest -q tests/test_paths.py \
  -k freecad_path_honors_override_without_side_effects
```

produced the genuine semantic RED:

```text
2 failed, 10 deselected
AttributeError: module 'vibecad.runtime.paths' has no attribute 'freecad_path'
```

### 30.3 G1-C00P candidate and review evidence

The authorized `gpt-5.6-sol / high` implementation packet then made only the
two track-owned edits:

- `freecad_path()` composes the GUI path from the unchanged
  `active_runtime_prefix()` selection;
- POSIX resolves `<prefix>/bin/FreeCAD`;
- Windows resolves `<prefix>/Library/bin/FreeCAD.exe`;
- the focused parameterized test uses a nonexistent override prefix and proves
  both exact paths without creating that prefix.

Candidate SHA-256 values:

```text
src/vibecad/runtime/paths.py
  36bc29d8de6d6757bbdddef08efbe4e28784139a2fb72aa127716ce1a09a423d
tests/test_paths.py
  4561cbc25d2c4bb22d36ec47592ec6b5b309ac2275fc629395b8a54195a97c97
```

Corrected G1-G00P GREEN:

```text
PYTHONPATH=src .venv/bin/python -m pytest -q tests/test_paths.py
12 passed

.venv/bin/python -m ruff check \
  src/vibecad/runtime/paths.py tests/test_paths.py
All checks passed!

git diff --check -- \
  src/vibecad/runtime/paths.py tests/test_paths.py
PASS
```

The implementation packet left the index empty and did not edit this artifact,
stage, commit, push, install, probe or launch FreeCAD. An independent
`gpt-5.6-sol / max` adversarial review inspected the exact candidate and
directly necessary tracked path/platform conventions:

```text
blocker:  0
critical: 0
major:    0
minor:    0
decision: GO
focused pytest: 12 passed
Ruff: PASS
diff-check: PASS
index: empty
```

The reviewer confirmed that C00P needs no package export, changes no existing
caller or `freecadcmd_path()`, and makes no existence, verification,
installation or launch claim. Residuals remain explicit:

- Windows is covered by branch simulation, not a native Windows host;
- GUI-binary regular-file identity and execute permission are required at every
  real caller and are deferred to the named real-GUI gates;
- no FreeCAD launch occurs in C00P.

Exact serial allowlist:

```text
M docs/orchestrated/vibecad-multi-runtime-g1.md
M src/vibecad/runtime/paths.py
M tests/test_paths.py
```

Exact subject:

```text
feat(runtime): expose managed FreeCAD GUI path
```

The concurrent MR1-P00 candidate remains unstaged in its disjoint approved
documentation write domain. It is not part of this commit and cannot be staged
with it. The five dynamic course scripts remain untracked, excluded and
untouched.

### 30.4 Recovery Snapshot MRG1-S10

#### S10-1 — Completed milestones

- MRG1-A04 was persisted and pushed at `4d92d04`; HEAD and upstream were equal
  before implementation began.
- MRG1-GATE-CORR-01 converted only the inherited source-checkout test
  projection and exposed a genuine focused C00P RED.
- G1-C00P reached focused GREEN and independent adversarial `0/0/0/0` GO on
  the exact two-file source candidate.
- MR1-P00 continues independently in its four-path documentation domain.

#### S10-2 — Ordered next packets and branch conditions

1. Run an independent `gpt-5.6-terra / medium` pre-stage gate over the exact
   three-path G1-C00P projection, candidate hashes, corrected command,
   allowlist, empty index, disjoint MR1 candidate and dynamic exclusions.
2. If GREEN, stage exactly the three G1-C00P allowlist paths and run a second
   cached-only mechanical projection. Any additional staged path, byte drift,
   course-script contact or gate mismatch is a breaker.
3. Commit with the exact C00P subject, push, and verify HEAD/upstream equality
   and ahead/behind `0/0`. Its final hash and push fact belong in the next
   semantic ledger preamble.
4. Independently review and gate MR1-P00, append its ledger, then stage only
   its five-path allowlist. It may not absorb G1 bytes.
5. Only after both first packets are independently committed and pushed may
   the controller dispatch G1-C00B and MR1-P01.

#### S10-3 — Active decisions and authorization

A01..A04 and D17..D22 remain active only within their named stages. FreeCAD is
the product priority; second-CAD implementation remains deferred. MR1-P00 is a
contract-only immutable-v1 preparation packet. No packaging, v2 writer,
release, external credentials/spend or other excluded feature is activated.
The Section 28.7 shared beta gate remains mandatory before the first promised
non-disposable external project.

#### S10-4 — Execution discipline

The selected capability profile remains:

```text
approval: artifact-approval
delegation: spawn-send-wait
persistence: repo-artifact
process: native-session-poll
```

The same four evidence categories recorded in S09 remain observable: live
capability declarations expose the controller, patch/command, agent and native
session tools; this campaign has observed authorization persistence,
sol/high implementation and sol/max review; the environment identity remains
Codex desktop `/root` in this repository; public configuration still exposes
Default mode, the current filesystem/approval profile and sol/terra routing.
No applicable `AGENTS.md` or `CLAUDE.md` has appeared.

All pytest rows now inherit MRG1-GATE-CORR-01. `standard`, `deep` and `fast`
remain sol/high, sol/max and terra/medium. One controller-owned index and one
exact semantic allowlist are permitted at a time. Recovery reads this artifact
and verifies HEAD/upstream/status/index, candidate hashes, corrected gate,
review evidence, dynamic exclusions and the first unclosed packet; chat memory
alone is never sufficient.

| Entry ID | Decision / approval | Commit / push | Gate evidence | Residual | Snapshot | State |
|---|---|---|---|---|---|---|
| MRG1-G1-C00P-E01 | A04; D19 FreeCAD-first path preparation | prior A04 `4d92d04` pushed; C00P `not-created` | corrected RED 2 failed; GREEN 12 passed; Ruff/diff PASS; sol/max 0/0/0/0 GO | native Windows and real GUI-binary identity/execution remain for later named gates; inherited residuals remain OPEN | MRG1-S10 | candidate GREEN / fast pre-stage gate next |

### 30.5 G1-C00P pre-stage mechanical PASS

An independent `gpt-5.6-terra / medium` agent mechanically verified the exact
pre-stage projection:

```text
branch:             codex/agent-stage3
HEAD/upstream:      4d92d04... / equal
ahead/behind:       0 / 0
index:              empty

G1 tracked paths:   exact three-path C00P allowlist
MR1 tracked paths:  exact disjoint approved documentation domain / unstaged
other tracked:      0
dynamic exclusions: exact five / untracked / names only

HEAD artifact SHA-256:
  5e122022180a6d8b9292aed96e476a37c445129dc18af65aa61e3b6348ceffe5
reviewed candidate artifact SHA-256:
  b76e1d755c41c2bd319334b2974f1a2bd56f790338b586aae92ffa8462d4cc4a
artifact delta:     222 additions / 0 deletions
strict byte append: PASS

paths.py SHA-256:
  36bc29d8de6d6757bbdddef08efbe4e28784139a2fb72aa127716ce1a09a423d
test_paths.py SHA-256:
  4561cbc25d2c4bb22d36ec47592ec6b5b309ac2275fc629395b8a54195a97c97

corrected pytest:   12 passed
Ruff:               PASS
diff-check:         PASS
```

The agent also confirmed the A04 push record, exact
`MRG1-GATE-CORR-01`, genuine corrected RED, candidate hashes, adversarial
`0/0/0/0`, exact subject/allowlist and all four S10 sections. It made zero
writes and did not read or contact excluded course-script content.

Exact named staging of the three C00P allowlist paths is now authorized. The
next gate is cached-only: cached path equality, cached/worktree byte identity,
strict artifact append, candidate hashes, corrected pytest, Ruff,
`git diff --cached --check`, disjoint unstaged MR1 projection and exact dynamic
exclusions. No additional path may enter the index.

| Entry ID | Decision / approval | Commit / push | Gate evidence | Residual | Snapshot | State |
|---|---|---|---|---|---|---|
| MRG1-G1-C00P-E02 | A04; D19 FreeCAD-first path preparation | prior A04 `4d92d04` pushed; C00P `not-created` | sol/max 0/0/0/0 GO; terra pre-stage PASS; corrected pytest 12; Ruff/diff PASS; hashes and strict append bound | native Windows and real GUI-binary identity/execution remain for later named gates; inherited residuals remain OPEN | MRG1-S10 | pre-stage GREEN / exact staging then cached-only gate |

## 31. G1-C00P Closeout and MR1-P00 Candidate Ledger

### 31.1 G1-C00P cached gate, commit and push

The exact three-path C00P index passed the independent cached-only
`gpt-5.6-terra / medium` projection:

```text
cached paths:
  M docs/orchestrated/vibecad-multi-runtime-g1.md
  M src/vibecad/runtime/paths.py
  M tests/test_paths.py
extra cached paths:  0
unstaged C00P:       0

cached/worktree Git object:
  artifact 711dcc6ac2f88565bf90e1f9685a25799e0af6a7 / equal
  paths    cf4dbacbced6b7e65e2a8475413605e68ed58b75 / equal
  tests    2ad33f55bd6bbc73bbb8696af3169058a9980ba1 / equal

artifact strict append: PASS
staged artifact SHA-256:
  0c41766f760f2be145689e2d9974a56dd15f9a9810af7be6e6f684d6ddff6f40
paths.py SHA-256:
  36bc29d8de6d6757bbdddef08efbe4e28784139a2fb72aa127716ce1a09a423d
test_paths.py SHA-256:
  4561cbc25d2c4bb22d36ec47592ec6b5b309ac2275fc629395b8a54195a97c97

corrected pytest:     12 passed
Ruff:                 PASS
cached diff-check:    PASS
dynamic exclusions:  exact five / names only / unstaged
```

The exact semantic commit was then created and pushed:

```text
commit: 50220446b851f8c0008dea4405cd09a3dadee11b
subject: feat(runtime): expose managed FreeCAD GUI path
push: origin/codex/agent-stage3
post-push HEAD/upstream: equal
ahead/behind: 0 / 0
index: empty
```

This closes G1-C00P. Real binary identity/execute-permission validation and
native-host launch remain intentionally deferred to C00B/C01 and their named
real gates; C00P alone makes no readiness or launch claim.

### 31.2 MR1-P00 prewrite audit and candidate

The authorized `gpt-5.6-sol / max` P00 implementation packet audited the
anchored source before writing documentation:

```text
tests/fixtures/durable_v1/:                 absent
src/vibecad/execution/revision_codec.py:    absent
src/vibecad/application/durable_migration.py: absent
Revision store: strict schema v1 / fixed FCStd and STEP writer
managed checkout: separate record-family v1/v2 reader / current local v2 writer
canonical migration/readiness/rollback contract: absent
```

Because P00 is the approved documentation G0, this observable gap replaces a
fabricated pytest RED. The packet then changed only its four exact
documentation paths:

```text
M docs/ARCHITECTURE.md
M docs/ACCEPTANCE_TESTS.md
M docs/CAD_RUNTIME_ADAPTER_GUIDE.md
A docs/orchestrated/vibecad-durable-v2.md
```

The candidate freezes:

- record-family-local version semantics so checkout v2 cannot be presented as
  Revision durable-v2;
- immutable committed Revision v1 history and frozen v1 encodings without
  forbidding legitimate existing CAS/atomic operational state changes;
- the one strict absent-profile legacy FreeCAD FCStd/STEP mapping;
- an independent closed, exact-keyset, canonical-byte/checksum-domain future
  Revision profile codec, while leaving its exact v2 JSON bytes unauthorized;
- byte-exact v1 corpus, strict v1 dispatch, observational full-root inventory,
  future independently gated dual-reader, then a later fenced writer
  activation;
- mixed v1-to-v2 ancestry, unknown/hybrid fail-closed, no eager/in-place
  rewrite and mutation-negative read/list/compare/export/preview;
- downgrade fail-closed and whole-root preactivation restore as the only
  path back to a v1 writer after committed v2 state;
- `data/` root identity, `locks/` and all five record stores in P03
  observation, with non-creating/non-lease-taking hooks and path-free output;
- same-live-tree identity equality separately from backup/restore logical,
  hash and reference equivalence with independent restored object identity;
- `structurally_ready` separately from future fenced `activation_ready`;
- disposable or independently export-verified G1 alpha separately from the
  shared non-disposable beta gate;
- unchanged 28 tools, six operations, `SelectorV1`, artifact URI and
  FreeCAD-only product scope.

Candidate SHA-256:

```text
docs/ARCHITECTURE.md
  93f6003f1a8e4a53f8cb4d882efd5cb10b1b19c28bb8cc8bd3ad7db57dd289ed
docs/ACCEPTANCE_TESTS.md
  e7de89fa2eac893e8e53dcc25335223f8544c4def7303fac2d75c95322e82023
docs/CAD_RUNTIME_ADAPTER_GUIDE.md
  812cb0711c3c19b96730021c41298efdc1b138fe9d36f572adb010dbb1a3ea8d
docs/orchestrated/vibecad-durable-v2.md
  c750de0f0a212091bd9b2ad886aa2ed3d7e4b86d912eb65879645dec40c08d0d
```

MRG1-GATE-CORR-01 projects MR1-G00 as:

```text
git diff --check &&
PYTHONPATH=src .venv/bin/python -m pytest -q tests/test_agent_skill.py
```

Candidate evidence:

```text
exit:                 0
tests:                12 passed
new-file whitespace:  PASS
relative links:       40 checked / 0 missing
Section 28 fragments: PASS
Markdown tables:      19 / 0 issues
required terms:       PASS
stale/overclaim audit: PASS
source/test diff:     0
index:                empty
```

### 31.3 MR1-P00 independent adversarial GO

An independent `gpt-5.6-sol / max` reviewer inspected the complete four-file
candidate, directly necessary anchored implementation facts and Sections
28–30. Initial and final candidate hashes were identical:

```text
blocker:  0
critical: 0
major:    0
minor:    0
decision: GO

corrected MR1-G00: 12 passed
diff-check:         PASS
relative links:     40 / 0 missing
tables/fences:      PASS
HEAD/upstream:      5022044... / equal / 0/0
index:              empty
```

The review specifically challenged whether P00 improperly deferred the exact
v2 JSON bytes. It found the deferral correct: P00 freezes the mandatory
independent schema version, stable domain/profile identity, exact artifact
roles/formats/media/cardinality/payload binding, independent checksum domain,
duplicate-key rejection, exact keyset and unknown-field fail-closed
invariants. The exact future keyset/encoding/encoder remains unauthorized and
must be frozen in a separately approved reader packet before any writer
activation. This neither permits permissive decode nor combines first-reader
and first-writer delivery.

The reviewer confirmed zero candidate/source/test writes, staging, commit,
push, install or FreeCAD launch. Excluded course scripts were observed only as
names in status and were not opened or contacted.

### 31.4 Clarified future blockers

The P00 audit established two new observable residuals:

- `DV2-RES-08`: Section 28.5's phrase “P04 and later v2 reader/writer
  activation” is not authority to combine first v2 reading with first v2
  writing. No future implementation packet may start until a controller
  append proposes exact separate reader-only and later fenced-writer commits,
  each with its own allowlist, approval and gate. This ledger fixes the
  interpretation but does not choose, budget or authorize those future
  commits, so the residual remains OPEN.
- `DV2-RES-09`: current Task snapshot and Revision discovery acquire
  catalog/quota leases whose first acquisition may persist lock files. P03
  cannot use them as a mutation-negative baseline or bypass store authority.
  If reviewed non-creating hooks require `workflow/store.py` or another
  unnamed path, the approved P03 packet stops for a new exact allowlist and
  approval. The residual remains OPEN.

The current P00 commit does not repair either future implementation concern.
P01 may proceed after P00 is pushed because it writes only reviewed fixtures
and one corpus test. P02 remains strict v1-only dispatch. Before dispatching
P03, the controller must reconcile `DV2-RES-09` against its exact path budget;
before any later dual-reader work, it must close the approval branch in
`DV2-RES-08`.

Exact serial allowlist:

```text
M docs/orchestrated/vibecad-multi-runtime-g1.md
M docs/ARCHITECTURE.md
M docs/ACCEPTANCE_TESTS.md
M docs/CAD_RUNTIME_ADAPTER_GUIDE.md
A docs/orchestrated/vibecad-durable-v2.md
```

Exact subject:

```text
docs(mr1): freeze durable-v2 migration contract
```

### 31.5 Recovery Snapshot MRG1-S11

#### S11-1 — Completed milestones

- G1-C00P passed corrected RED/GREEN, sol/max review and both terra gates, then
  committed and pushed at `50220446`; HEAD/upstream are equal.
- MR1-P00 completed its documentation-gap audit, four-file contract candidate,
  corrected G00 and independent adversarial `0/0/0/0` GO.
- No v2 implementation, inventory, activation, backup, fence, second CAD or
  public schema change exists.

#### S11-2 — Ordered next packets and branch conditions

1. Run an independent `gpt-5.6-terra / medium` pre-stage gate over the exact
   five-path P00 projection, candidate hashes, corrected command, links/tables,
   empty index and dynamic exclusions.
2. If GREEN, stage exactly the five P00 allowlist paths and run a second
   cached-only projection. Any missing/extra path, byte mismatch or new
   candidate drift is a breaker.
3. Commit with the exact P00 subject, push and verify HEAD/upstream equality
   and ahead/behind `0/0`. Record its final hash/push in the next semantic
   ledger preamble.
4. Only after that push, dispatch G1-C00B at sol/high and MR1-P01 at sol/max
   into their disjoint exact write domains.
5. C00B must resolve its approved real-probe branch from observable evidence;
   P01 may freeze bytes but has no update-golden mode. Neither can absorb an
   unnamed correction or the other's files.

#### S11-3 — Active decisions and authorization

A01..A04, D17..D22 and DV2-D01..D12 remain active only in their named stages.
The first-product priority is still end-to-end FreeCAD. Second CAD, v2
reader/writer activation, packaging/release and all other exclusions remain
unauthorized. `DV2-RES-08/09` are active blockers at their future boundaries,
not waivers or scope expansions. The shared non-disposable beta gate remains
mandatory.

#### S11-4 — Execution discipline

Capability profile remains artifact-approval / spawn-send-wait /
repo-artifact / native-session-poll under the same four S09 evidence
categories. Model routing remains terra/medium for mechanics, sol/high for
routine implementation and sol/max for durable architecture, research and
adversarial review. MRG1-GATE-CORR-01 applies to every pytest row.

One exact semantic allowlist and one controller-owned index are permitted at a
time. No broad add, golden rewrite, test weakening, retry-until-green,
unrecorded correction or user-file contact is permitted. Recovery verifies
HEAD/upstream/status/index, the last pushed hash, candidate hashes, P00
contract/review/gate evidence, dynamic exclusions, the capability profile and
the first OPEN residual from repository artifacts; chat memory alone is not a
recovery source.

| Entry ID | Decision / approval | Commit / push | Gate evidence | Residual | Snapshot | State |
|---|---|---|---|---|---|---|
| MRG1-MR1-P00-E01 | A04; D21 MR1-prep in parallel with FreeCAD G1 | prior G1-C00P `50220446` pushed; P00 `not-created` | G0 gap audit; corrected MR1-G00 12 passed; links/tables/diff PASS; sol/max 0/0/0/0 GO; candidate hashes bound | DV2-RES-01..09 OPEN/DEFERRED under exact owners; inherited residuals remain OPEN | MRG1-S11 | candidate GREEN / fast pre-stage gate next |

### 31.6 MR1-P00 pre-stage mechanical PASS

The exact P00 candidate passed an independent `gpt-5.6-terra / medium`
pre-stage projection:

```text
branch:             codex/agent-stage3
HEAD/upstream:      50220446... / equal
ahead/behind:       0 / 0
index:              empty
tracked paths:      artifact + exact three canonical docs
new P00 path:       exact durable-v2 artifact
source/test diff:   0
dynamic exclusions: exact five / names only

HEAD artifact:
  263647 bytes
  0c41766f760f2be145689e2d9974a56dd15f9a9810af7be6e6f684d6ddff6f40
reviewed candidate artifact:
  274610 bytes
  dd5493a5f5e9c3ae1808a2ecc85b263dfd6ea0c2febcb275cee83f7a07f63122
strict append:      PASS / 10963 bytes

canonical hashes:   exact four candidate SHA-256 values / PASS
corrected MR1-G00:  12 passed
tracked diff-check: PASS
new-file check:     PASS
relative links:     40 / 0 missing
fragments:          2 / 0 missing
Markdown tables:    87 / 0 issues
fence/final newline: PASS
overclaim audit:    PASS
```

The mechanical agent confirmed all required Section 31 records, exact
allowlist/subject and S11-1..S11-4. It found no v2 implementation,
activation-ready or durable-beta PASS claim and made zero writes. Excluded
course-script content was not opened or contacted.

Exact named staging of the five P00 allowlist paths is now authorized. The
cached-only gate must re-prove the exact staged path set, cached/worktree byte
identity, no unstaged P00 delta, strict artifact append, all five hashes,
corrected MR1-G00, diff/link/table/fence checks, source/test zero and the exact
dynamic exclusions.

| Entry ID | Decision / approval | Commit / push | Gate evidence | Residual | Snapshot | State |
|---|---|---|---|---|---|---|
| MRG1-MR1-P00-E02 | A04; D21 MR1-prep in parallel with FreeCAD G1 | prior G1-C00P `50220446` pushed; P00 `not-created` | sol/max 0/0/0/0 GO; terra pre-stage PASS; corrected G00 12; links/tables/fences/diff/hashes PASS | DV2-RES-01..09 OPEN/DEFERRED under exact owners; inherited residuals remain OPEN | MRG1-S11 | pre-stage GREEN / exact staging then cached-only gate |

## 32. MR1-P00 Closeout and G1-C00B Harness Recovery

### 32.1 MR1-P00 cached gate, commit and push

The exact five-path P00 index passed its independent cached-only
`gpt-5.6-terra / medium` projection:

```text
cached paths:        exact five-path P00 allowlist
extra cached paths:  0
unstaged P00:        0
cached/worktree:     byte-identical for all five

staged artifact SHA-256:
  07d89afc4be2d9af0e3761c45ebdbcc4aa019151c1f568b615461624fad1bc50
artifact strict append: PASS
canonical candidate hashes: exact four / PASS
corrected MR1-G00:  12 passed
cached diff-check:  PASS
links/fragments:    40 / 2 / 0 missing
tables/fences:      PASS
source/test diff:   0
dynamic exclusions: exact five / names only
```

The exact semantic commit was then created and pushed:

```text
commit: 2cfbbc416d789491c1c532653b4e460c53dfac60
subject: docs(mr1): freeze durable-v2 migration contract
push: origin/codex/agent-stage3
post-push HEAD/upstream: equal
ahead/behind: 0 / 0
index: empty
```

This closes MR1-P00. Corpus, codec, inventory and every future activation
residual remain OPEN under the owners and breakers recorded in Sections
31.4–31.5.

### 32.2 C00B first real-host setup breaker

The authorized C00B packet verified the exact managed runtime receipt, prefix
generation and executable identity, then invoked one bounded real child with
both required paths:

```text
<verified-prefix>/bin/freecadcmd
  -P <repo>/src
  -P <repo>/tests/fixtures/freecad_workbench
  <repo>/tests/fixtures/freecad_workbench/bootstrap_probe.py
```

The first child, pid `59431`, loaded and ran the exact probe script but
resolved `vibecad` and `daemon/bootstrap.py` from the managed installation's
site-packages rather than the current repository. It exited 1 with:

```text
RuntimeError: vibecad source identity mismatch
```

This happened before `LocalAgentClient.open()`. No daemon id, daemon pid,
socket or run-root publication existed. The child was dead, `ps` and bounded
daemon-name checks were empty, and the isolated run root/socket were absent.
Therefore this was a real-host harness/source-precedence setup breaker, not
the approved embedded daemon-launch semantic RED. It did not authorize a
production correction.

An independent `gpt-5.6-sol / max` audit found that this FreeCAD host's `-P`
made the repository paths searchable but did not outrank managed
site-packages. It approved `MRG1-C00B-HARNESS-CORR-01` within the two already
named test paths:

- derive the repository only from the exact resolved probe `__file__`;
- verify the exact fixture and regular source identities;
- fail if any `vibecad` module is already loaded; never delete/reload it;
- put the deduplicated exact repo source at `sys.path[0]`, invalidate caches
  and verify exact `vibecad.__file__` and bootstrap source identities;
- preserve both `-P` arguments, bounded output and cleanup evidence.

No global `PYTHONPATH`, sitecustomize, install, production or acceptance
change was permitted.

### 32.3 C00B second real-host setup breaker

After the first narrow harness correction, one authorized re-characterization
ran. Repository source identity succeeded:

```text
child pid:            60514
return code:          1
timed out:            false
stderr:               empty
bootstrap source:     exact <repo>/src/vibecad/daemon/bootstrap.py
preloaded vibecad:    none
cold run root:        absent
error:
  ValueError: runtime write directory is unavailable
```

The isolated home had been created with a `/tmp/...` spelling. On this macOS
host `/tmp` is an alias for the canonical `/private/...` tree, and the
identity-pinned runtime traversal correctly rejected the alias. The failure
again occurred before a daemon id/pid/socket was published and before the
characterized child command could be assessed. Child `60514` was dead; no
daemon process, socket, run root or temporary root remained. Production
remained byte-identical:

```text
src/vibecad/daemon/bootstrap.py
  b1c9b3e37b0f3d1de7551b5c6921e21057f12f0d67b75d88b2dcbd60b4494eec
tests/test_p0b_acceptance.py
  b1d70fa8a064a8a993b49674bcd2cd595fb6bb9d98d670276bd1994e9d72f975
```

Harness candidate after the second stop:

```text
tests/fixtures/freecad_workbench/bootstrap_probe.py
  fde0c459f96fc91721c7036a036fbe09c8cf8d768171f1f82e82113da1f3f3fd
tests/test_freecad_workbench_bootstrap.py
  adf960d4a788f50561e7ba796f608f625e51f5cac6889aeba6b43bc760d5743a
focused non-real: 1 passed / 1 deselected
Ruff: PASS
diff-check: PASS
index: empty
```

This second observable cause is distinct from source precedence and remains a
setup breaker, not daemon semantic RED. No production branch or commit subject
has been selected.

### 32.4 MRG1-C00B-HARNESS-CORR-02 ruling

The independent sol/max audit returned a bounded non-expansion ruling:
MRG1-A04 may continue without a new user approval only under all of these
conditions:

1. correction bytes remain limited to the two already authorized new
   test/harness paths plus this controller artifact;
2. the parent creates the temporary root beneath a canonical resolved macOS
   temporary parent, then requires exact path spelling equal to strict
   resolution, current-user ownership, directory kind and owner-private mode;
3. every `VIBECAD_HOME`, FreeCAD user root and child temp path is derived from
   that one canonical pinned spelling; `/tmp/...` and `/private/...` are never
   mixed;
4. deterministic non-FreeCAD tests prove canonical-root admission before any
   next real invocation;
5. a sol/max review and terra/medium mechanical gate bind the artifact,
   candidate hashes, both setup failures, zero-leak evidence and the exact
   one-shot budget before execution;
6. exactly one third real invocation is permitted. It is the last setup
   recovery attempt under A04; no fourth invocation is allowed;
7. if the third invocation reaches `LocalAgentClient.open()` and reproduces
   the `[sys.executable, -B, -m, vibecad.daemon]` embedded-launch failure, that
   is the first authorized semantic RED and may select the already approved
   narrow production correction branch;
8. any third setup/source/identity/process/cleanup failure, any unnamed path,
   global environment/acceptance change or production edit before semantic
   RED stops C00B for a new controller append and explicit user approval.

This is not retry-until-green: neither prior child reached the behavior under
characterization; each fail-closed cause is distinct and observable; candidate
bytes and deterministic admission evidence must change before the only
remaining attempt; the attempt budget is now explicit and finite. Runtime
identity validation is not weakened or bypassed.

The next implementation action may only add the canonical owner-private root
helper/assertions in `tests/test_freecad_workbench_bootstrap.py` and run
non-real tests/Ruff/diff. It must stop before real FreeCAD until the independent
pre-real review and mechanical gate are GREEN.

### 32.5 Recovery Snapshot MRG1-S12

#### S12-1 — Completed milestones

- G1-C00P and MR1-P00 are committed and pushed through `2cfbbc416`.
- C00B produced two cleanly contained, pre-semantic real-host setup breakers:
  import precedence, then macOS temporary-path aliasing.
- MRG1-C00B-HARNESS-CORR-01 fixed only source selection. No production or
  public behavior changed.
- MR1-P01 continues independently in its exact fixture/test domain.

#### S12-2 — Ordered next packets and branch conditions

1. Apply only the canonical-root test correction and deterministic non-real
   assertion; run focused non-real pytest, Ruff, diff, status and hashes.
2. Independently review the exact artifact + two-test-path pre-real candidate
   at sol/max, then mechanically gate it at terra/medium.
3. If both are GREEN, run the one remaining real invocation and poll only its
   original session.
4. Direct real GREEN selects
   `test(workbench): prove embedded daemon bootstrap` with production diff 0.
   A genuine daemon-launch RED selects the already approved narrow
   `fix(daemon): bind embedded bootstrap to managed Python` branch, which must
   reach real GREEN and pass its regression/adversarial/mechanical gates.
5. Any other result blocks C00B and consumes the final attempt. Preserve
   evidence; do not delete the candidate or retry.
6. MR1-P01 may independently proceed to review/gates/commit. The controller
   serializes indexes and commits.

#### S12-3 — Active decisions and authorization

A01..A04 and D17..D22 remain active only in their exact stages.
MRG1-C00B-HARNESS-CORR-02 changes no product claim, source allowlist,
acceptance criterion, environment contract or commit budget. It grants only
one pre-gated real setup recovery attempt. Second CAD, packaging/release and
all other exclusions remain unchanged.

#### S12-4 — Execution discipline

Capability profile and four evidence categories remain those in S09/S11.
Critical real-process boundary review uses sol/max; mechanical projection uses
terra/medium. MRG1-GATE-CORR-01 still supplies `PYTHONPATH=src` to the parent
pytest command; it does not replace the child source-identity proof.

One controller-owned index remains empty until a complete semantic candidate
is ready. Long processes use their one native session and are never duplicate
launched. Exact-daemon cleanup requires identity-bound retirement; no broad
kill is permitted. Recovery verifies both child pids are dead, no daemon
process/socket/run root exists, source/production hashes, candidate hashes,
HEAD/upstream/status/index, dynamic exclusions and the remaining one-shot
budget from this artifact.

| Entry ID | Decision / approval | Commit / push | Gate evidence | Residual | Snapshot | State |
|---|---|---|---|---|---|---|
| MRG1-G1-C00B-E01 | A04 C00B; test-only harness corrections under existing allowlist | prior P00 `2cfbbc416` pushed; C00B `not-created` | setup 1 source mismatch / zero leak; sol/max CORR-01 GO; setup 2 canonical-path rejection / zero leak; focused 1 passed; Ruff/diff PASS; production hashes unchanged | one pre-gated real attempt remains; any non-semantic failure requires new explicit approval; all inherited residuals remain OPEN | MRG1-S12 | blocked before real behavior / CORR-02 test bytes and pre-real gates next |

### 32.6 C00B final pre-real review and mechanical PASS

The locked CORR-02 candidate passed independent adversarial review:

```text
model:     gpt-5.6-sol / max
blocker:   0
critical:  0
major:     0
minor:     0
decision:  GO for exactly one final real invocation
non-real:  2 passed / 1 deselected
Ruff:      PASS
```

The audit mapped the anchored client/bootstrap/retirement contracts and
confirmed that the startup path bounds and cleans an unpublished losing child,
while the parent additionally reads any partial exact boot receipt and retires
only its authenticated daemon id. FreeCAD's primary implementation appends
each `-P` value to `sys.path`; it does not promise precedence. The
fixture-derived, pre-import `sys.path[0]` correction is therefore required to
test current checkout bytes and does not weaken the two retained `-P`
requirements.

The reviewer accepted the canonical temporary-root admission, preload/source
identity checks, bounded child/timeout behavior, partial-publication handling,
expected-daemon-only retirement and no-fourth governance. It found no
production, environment-contract, acceptance or path-scope expansion.

An independent `gpt-5.6-terra / medium` pre-real gate then returned PASS:

```text
branch/HEAD/upstream: codex/agent-stage3 / 2cfbbc416... / equal
ahead/behind:         0 / 0
index:                empty

reviewed artifact SHA-256:
  d6d5b41f56ca0bbffee24662aa898becabc67bb1732c41da392ec2920afc0664
strict append:
  276700-byte HEAD prefix / PASS
  10237 appended bytes

probe SHA-256 start/end:
  fde0c459f96fc91721c7036a036fbe09c8cf8d768171f1f82e82113da1f3f3fd
parent SHA-256 start/end:
  6713918f880db77a83914b204cbc01fbb1e3b5204d20a32344e96d9a8c0e95a2
production hashes:   exact unchanged pair / no diff
canonical admission: PASS
non-real pytest:     2 passed / 1 deselected
Ruff/diff/new-file:  PASS
P01 domain:          disjoint / unstaged
dynamic exclusions: exact five / names only
integration launch:  none
```

The only remaining execution allowance is one real invocation on the bound
candidate hashes. Direct GREEN selects the test-only subject. Only a failure
that reaches the characterized embedded
`[sys.executable, -B, -m, vibecad.daemon]` launch may select the already
approved production-correction branch. Any other failure, timeout, leak,
candidate/artifact drift or unknown process consumes the attempt and stops
C00B for a new explicit approval.

Before that invocation, one final fast agent must verify that this evidence
append is itself a strict append and that the artifact/test/production hashes,
index, HEAD/upstream and one-shot budget remain unchanged. It must not launch
FreeCAD.

| Entry ID | Decision / approval | Commit / push | Gate evidence | Residual | Snapshot | State |
|---|---|---|---|---|---|---|
| MRG1-G1-C00B-E02 | A04 C00B; CORR-02 exact final one-shot setup recovery | prior P00 `2cfbbc416` pushed; C00B `not-created` | sol/max 0/0/0/0 GO; terra pre-real PASS; non-real 2 passed/1 deselected; Ruff/diff/hashes/production-zero PASS | exactly one real invocation remains; no fourth; non-semantic failure requires new explicit approval | MRG1-S12 | pre-real GREEN / final evidence-byte check then one real invocation |

## 33. G1-C00B Semantic RED and Conditional Correction Branch

### 33.1 Final locked real invocation reached the characterized behavior

After the Section 32.6 evidence append, a final independent fast byte check
returned PASS:

```text
final pre-real artifact SHA-256:
  f3059e8a2f6925c31e15cd2579f5f54352b4b2ae2f6921946deb79466b9038cd
strict append to 276700-byte HEAD artifact: PASS
probe/parent/production hashes: exact locked values / stable
HEAD/upstream: 2cfbbc416... / equal / 0/0
index: empty
non-real: 2 passed / 1 deselected
Ruff/diff: PASS
integration launch during byte gate: none
```

The one remaining unchanged-candidate characterization then ran exactly once:

```text
VIBECAD_RUN_INTEGRATION=1
VIBECAD_FREECAD_ENV=<verified-absolute-managed-prefix>
PYTHONPATH=src
.venv/bin/python -m pytest -q -m slow
  tests/test_freecad_workbench_bootstrap.py
```

Observable result:

```text
duration:             15.52 seconds
FreeCAD child pid:    62452
child return:         1
timed out:            false
stderr:               empty
probe JSON:           one unambiguous object
probe/bootstrap:      exact repository identities
canonical temp root:  admitted
preloaded vibecad:    none
cold run root:        absent
reached call:         LocalAgentClient.open()
error:
  DaemonError: The local daemon is unavailable.
```

The 15-second interval matches `DAEMON_BOOTSTRAP_TIMEOUT_SECONDS`. The probe
executed the current `_spawn_daemon()` path whose fixed command is:

```text
[sys.executable, "-B", "-m", "vibecad.daemon"]
```

Inside the embedded interpreter `sys.executable` denotes the FreeCAD host, not
the verified managed Python. No authenticated daemon receipt was ever
published; therefore no daemon id/pid/process command or valid retirement
target existed. Parent leak assertions passed before the semantic RED was
reported:

```text
child 62452:          dead / ps absent
FreeCAD processes:    none from the isolated run
vibecad.daemon:       none
kernel socket:        absent
daemon run root:      absent
canonical temp root: removed
retired:              false / no published target
```

The final invocation consumed the setup-characterization budget and no retry
occurred. All locked bytes remained unchanged:

```text
tests/fixtures/freecad_workbench/bootstrap_probe.py
  fde0c459f96fc91721c7036a036fbe09c8cf8d768171f1f82e82113da1f3f3fd
tests/test_freecad_workbench_bootstrap.py
  6713918f880db77a83914b204cbc01fbb1e3b5204d20a32344e96d9a8c0e95a2
src/vibecad/daemon/bootstrap.py
  b1c9b3e37b0f3d1de7551b5c6921e21057f12f0d67b75d88b2dcbd60b4494eec
tests/test_p0b_acceptance.py
  b1d70fa8a064a8a993b49674bcd2cd595fb6bb9d98d670276bd1994e9d72f975
```

This is the first genuine C00B embedded daemon-launch semantic RED. It is not
a setup failure, identity mismatch, environment repair request or cleanup
ambiguity.

### 33.2 Approved conditional branch selected

The exact observable condition in Section 28.4 is now satisfied. The
authorized branch and subject are selected:

```text
branch:  reproduced RED plus narrow correction
subject: fix(daemon): bind embedded bootstrap to managed Python
```

Exact serial allowlist remains:

```text
M docs/orchestrated/vibecad-multi-runtime-g1.md
A tests/fixtures/freecad_workbench/bootstrap_probe.py
A tests/test_freecad_workbench_bootstrap.py
M src/vibecad/daemon/bootstrap.py
M tests/test_p0b_acceptance.py
```

This selection requires no new user approval because MRG1-A04 explicitly
approved this mutually exclusive C00B branch, these exact production/regression
paths and this subject after a real reproduced embedded-launch RED. It does not
authorize another setup correction, a new path, a second daemon/Application/
Task Kernel, install/config change or broader launcher refactor.

The correction may be implemented only after a `gpt-5.6-sol / max` design
review freezes:

- the exact identity-bound active managed-Python selection predicate;
- the development-Python fallback and its fail-closed embedded-host boundary;
- external-override receipt/binding behavior in the isolated real harness;
- unchanged daemon environment, cwd, startup-claim fd, session and cleanup
  semantics;
- the exact named regression that is RED on current production bytes.

Implementation then follows:

1. add only
   `test_embedded_freecad_uses_managed_python_for_cold_daemon` in the approved
   P0B test path;
2. run it on unchanged production and record the genuine deterministic RED;
3. make the smallest reviewed bootstrap correction;
4. run the regression, full affected P0B/C00B non-real suites and Ruff to
   GREEN;
5. obtain independent sol/max adversarial review and terra/medium pre-real
   mechanical gate on final hashes;
6. run one bounded **post-correction GREEN verification**, not another
   characterization/setup retry;
7. require exact daemon id/pid/process publication, client close, expected-id
   retirement and pid/socket/process/live-run-root absence.

The “no fourth setup invocation” rule remains closed: no unchanged/harness
characterization is permitted. The future post-correction real run is a
separately pre-gated verification of new production bytes under the
already-selected A04 branch. If it is not GREEN, C00B stops with no second
correction verification or retry.

### 33.3 Concurrent MR1-P01 state

MR1-P01 independently generated its reviewed corpus candidate and then stopped
on one test-only ordering assertion: it expected `index.json` before a
lexicographically sorted member list, while the actual first member is
`checkout_open_v1.json`. Six semantic/round-trip tests passed and all 31
repository fixture bytes match the one-time reviewed candidate. The agent did
not opportunistically edit the assertion after final bytes.

This P01 stop is disjoint from C00B and remains unstaged. It will receive its
own correction ruling, review and gate; it cannot enter the C00B index or
influence the selected daemon branch.

### 33.4 Recovery Snapshot MRG1-S13

#### S13-1 — Completed milestones

- G1-C00P and MR1-P00 are pushed through `2cfbbc416`.
- C00B setup admission is closed: source precedence and canonical macOS temp
  path both passed on the final locked candidate.
- The final characterization reached `LocalAgentClient.open()` and reproduced
  the exact existing embedded-launch defect with zero leaks.
- The A04 conditional managed-Python correction branch is now selected.
- P01 corpus bytes exist unstaged; its one failing ordering assertion remains
  unmodified.

#### S13-2 — Ordered next packets and branch conditions

1. Complete the sol/max managed-Python selection design review.
2. Dispatch the exact two-path production/regression correction at sol/high.
   Capture focused regression RED before production bytes.
3. Review/gate final non-real C00B bytes. Only then run one post-correction
   real GREEN verification; failure blocks with no retry.
4. In parallel or after the real gate, independently rule on the P01
   test-order assertion and permit only a test-only correction if it does not
   change fixture/index bytes or acceptance semantics.
5. The controller serializes candidate ledgers, exact indexes, cached gates,
   commits and pushes. C00B and P01 bytes never share one commit.

#### S13-3 — Active decisions and authorization

A01..A04 and D17..D22 remain active only at their exact stages. C00B is now on
its pre-approved correction branch. P01 remains characterization-only and
production-free. Second CAD, G1 addon/Dock work, P02, packaging/release and
all other later scopes remain excluded until their predecessor commits are
pushed.

#### S13-4 — Execution discipline

Capability profile, model routing and dynamic exclusions remain unchanged.
Real-process and correction-design review uses sol/max; routine implementation
uses sol/high; mechanical gates use terra/medium. MRG1-GATE-CORR-01 remains
active for parent pytest imports.

The controller-owned index remains empty. Recovery verifies the three observed
FreeCAD child pids, zero-leak evidence, selected branch/subject/allowlist,
locked pre-correction hashes, HEAD/upstream/status/index, P01 disjoint paths,
dynamic exclusions and the first unclosed design/gate from this artifact.
There is no broad kill, repeated characterization, update-golden, test
weakening or user-file contact.

| Entry ID | Decision / approval | Commit / push | Gate evidence | Residual | Snapshot | State |
|---|---|---|---|---|---|---|
| MRG1-G1-C00B-E03 | A04 conditional correction branch selected by exact semantic RED | prior P00 `2cfbbc416` pushed; C00B `not-created` | final byte gate PASS; real child 62452 reached LocalAgentClient.open; bounded 15.52s unavailable RED; zero leak; four locked hashes unchanged | managed-Python selection design/regression/non-real/review/real GREEN still required; one correction verification only | MRG1-S13 | semantic RED bound / sol/max correction design next |

### 33.5 Managed-Python correction design GO

The required independent `gpt-5.6-sol / max` design review returned GO and
froze one private selector in `daemon/bootstrap.py`. Only
`_spawn_daemon()`'s argv[0] may change.

Selection order:

1. A development interpreter is proven only when `sys.executable` is the same
   file as `paths.env_python_for(Path(sys.prefix))`. That exact running Python
   is retained.
2. Otherwise the caller is accepted only when `sys.executable` is the same
   file as the active `freecadcmd_path()` or `freecad_path()`. An unknown
   embedding host fails closed.
3. Embedded FreeCAD requires `status.runtime_ready()`, a stable active prefix
   across capture, exact `RuntimeGenerationEvidence` for that prefix and
   `env_python_for(prefix)`, a regular executable Python target, and proof that
   selected Python is not the FreeCAD host.
4. Missing/incompatible receipt, arbitrary unbound override, alias/identity
   drift, unavailable Python or unready embedded runtime raises before
   `Popen`. There is no embedded fallback to `sys.executable`.

Decision table:

| Context | Result |
|---|---|
| proven repository or venv Python | unchanged exact `sys.executable` |
| default managed FreeCAD + current receipt | exact captured active managed Python |
| external override FreeCAD + identity-bound external receipt | exact captured override Python |
| embedded FreeCAD + missing/incompatible receipt | fail before `Popen` |
| unknown non-Python embedding host | fail closed |
| arbitrary unbound override | never selected |

The helper does not import the supervisor, call `verify_runtime*()`, launch a
probe, install/repair anything or fall back after an embedded-host failure.
Existing stdin/stdout/stderr, safe environment, cwd, close-fd,
`start_new_session`, inherited startup-claim fd and `pass_fds` arguments remain
semantically identical. The existing runtime-maintenance claim surrounds
selection and spawn. A user-maintained external prefix can still be mutated
out of band; this is the pre-existing external-runtime residual, not a reason
to weaken the receipt/generation boundary.

The real harness must, after setting isolated `VIBECAD_HOME` and the verified
override, publish the external binding with the existing
`status.write_external_runtime_receipt(prefix, evidence=runtime_evidence)`
inside a bounded maintenance-lock scope, then require:

```text
paths.bound_external_prefix() == verified prefix
status.runtime_ready() is true
```

It must release that harness claim before FreeCAD launches so the embedded
client can acquire the normal claim. Hand-written receipt JSON is forbidden.
The parent also binds the observed daemon process to the recorded managed
Python.

The exact named regression must mock `Popen` and cover within one test:

- ready embedded FreeCAD selects
  `[managed_python, "-B", "-m", "vibecad.daemon"]`;
- unready embedded FreeCAD raises before `Popen`;
- proven venv Python with runtime unready retains `sys.executable`;
- an arbitrary unbound override cannot select its Python;
- every existing spawn kwarg and safe-environment/startup-claim fact remains
  unchanged.

On the pre-correction production bytes, the first assertion is deterministically
RED because argv[0] is the mocked FreeCAD host. The implementation packet must
stop before the post-correction real invocation; review and mechanical gates
own that authority.

### 33.6 MR1-P01 blocker review GO

An independent `gpt-5.6-sol / max` P01 review reproduced `6 passed / 1 failed`
and independently verified:

```text
index:            7921 bytes
index SHA-256:    b6cee09ee434b9e952e011534124b11f9f910b9d706570c3030a0c05c35cc432
indexed members:  30 / unique / exact directory coverage excluding index
repo fixtures:    31 / regular / non-symlink
candidate diff:   byte-identical to reviewed one-time temp candidate
hash/size fields: exact for all indexed members
canonical JSON:   PASS
cross-reference:  PASS
update mode:      absent
real credential/path: absent
```

The only semantic failure is the test's false assumption that `index.json`
sorts before every indexed member. The exact authorized assertion shape is:

```python
expected_paths = tuple(sorted(("index.json", *EXPECTED_MEMBERS)))
assert tuple(path.name for path in actual_paths) == expected_paths
```

All 31 fixture bytes, including `index.json`, are frozen and cannot change.
The same test file also requires removal of an unused import, Ruff import-order
cleanup and formatting; these are mechanical same-file corrections and cannot
weaken coverage.

Two approved binary fixture paths are ignored by repository patterns:

```text
tests/fixtures/durable_v1/model.FCStd
tests/fixtures/durable_v1/model.step
```

They remain required P01 bytes with frozen hashes:

```text
model.FCStd
  b8b93ace9ff2f0dff51c9e5affac2241522cde21ccea95b993269d6c2d688ecc
model.step
  e2fba839d0be4827a2c92e730f352f44ec1c3d9bacec021306847bc4f1fbc215
```

At the later P01 staging step the controller must use exact force-add for only
those two paths, then prove the cached path set equals the full approved P01
allowlist. No broad force-add or ignore-rule change is authorized.

| Entry ID | Decision / approval | Commit / push | Gate evidence | Residual | Snapshot | State |
|---|---|---|---|---|---|---|
| MRG1-G1-C00B-E04 | A04 conditional correction; exact selector/harness design frozen | prior P00 `2cfbbc416` pushed; C00B `not-created` | sol/max design GO; semantic RED and zero leak remain bound | regression RED/GREEN, non-real review/mechanical and one real GREEN still required | MRG1-S13 | design GREEN / sol-high two-path production correction next |
| MRG1-MR1-P01-E01 | A04 P01; immutable one-time corpus bytes | prior P00 `2cfbbc416` pushed; P01 `not-created` | initial missing-index RED; 31 fixtures generated once; 6 semantic tests pass; sol/max member/hash/provenance review GO | one test-order assertion plus same-file lint/format correction; exact force-add of two ignored payloads required | MRG1-S13 | corpus bytes frozen / one-test-file correction and gates next |

## 34. Independent release-workflow parser repair authorization

GitHub's run annotation for branch commit `5022044` reports that
`.github/workflows/release.yml` is invalid before job creation:

```text
(Line: 47, Col: 21): Unrecognized named-value: 'runner'
(Line: 116, Col: 21): Unrecognized named-value: 'runner'
```

Both invalid expressions are job-level environment values:

```yaml
VIBECAD_HOME: ${{ runner.temp }}/vibecad-release-runtime
RELEASE_DIST: ${{ runner.temp }}/release-dist
```

The `runner` context is not available while GitHub evaluates
`jobs.<job_id>.env`; consequently the tag-only workflow is rejected during
workflow validation and branch pushes produce a failed run with no jobs.

The controller presented the following exact repair to the user:

1. keep the `v*` tag trigger and all release behavior unchanged;
2. remove the two invalid job-level `runner.temp` expressions;
3. initialize `VIBECAD_HOME` and `RELEASE_DIST` from the runner-provided
   `$RUNNER_TEMP` inside the first step of their respective jobs by appending
   to `$GITHUB_ENV`;
4. validate the workflow mechanically and commit it independently from
   G1-C00B and MR1-P01.

The user explicitly replied `批准` on 2026-07-26. This authorization is bound
to the exact implementation allowlist `.github/workflows/release.yml`, the
subject `fix(ci): bind runner temp paths at step runtime`, and these gates:

- YAML syntax parse;
- no `runner` expression in any job-level `env`;
- the four legitimate step-level `runner.temp` artifact paths remain;
- exactly one runtime binding for each approved variable, before its first
  consumer;
- focused diff/whitespace inspection;
- independent mechanical review of the final bytes.

No notification setting, trigger, release permission, action version, product
source, test, fixture, package content or untracked course document may
change. The implementation agent cannot stage, commit or push; the controller
owns exact staging, cached gates, the commit and the immediate push. The
controller artifact remains controller-owned and is not part of this
independent CI commit while the earlier C00B/P01 evidence is unstaged.

### 34.1 Capability profile and adapter evidence

Selected adapter: Codex native adapter.

```text
approval: native-plan
delegation: spawn-send-wait
persistence: repo-artifact
process: native-session-poll
```

- `live capability declarations`: `update_plan`, `spawn_agent`,
  `followup_task`, `wait_agent`, `exec_command` and `write_stdin` are declared
  live; explicit model/reasoning selectors are declared on `spawn_agent`.
- `observable behavior`: native plan projection, subagent state observation
  and synchronous repository inspection have succeeded in this session;
  completed subagent reports are observable through `list_agents`.
- `environment identity`: Codex desktop exposes the repository root
  `/Users/wangtao/Documents/DevProject/vibecad`, branch
  `codex/agent-stage3`, macOS/zsh controller environment and current
  unrestricted filesystem profile.
- `public configuration`: collaboration mode is Default; the live tool
  declarations expose four total concurrency slots and controllable native
  sessions; the user requires routine coding at `gpt-5.6-sol / high`,
  mechanical gates at `gpt-5.6-terra / medium`, and critical review at
  `gpt-5.6-sol / max`.

Applicable directory-scoped `AGENTS.md` / `CLAUDE.md`: none observed in this
repository. The current host permission model and sandbox remain binding.

| Entry ID | Decision / approval | Commit / push | Gate evidence | Residual | Snapshot | State |
|---|---|---|---|---|---|---|
| MRG1-CI-REL-E01 | user approval on 2026-07-26; exact §34 repair | `not-created` | public run annotation identifies the two invalid job-level contexts; local pre-edit scan confirms lines 47 and 116 | `gh` CLI is not authenticated; local and public read-only evidence remains sufficient for implementation, but post-push confirmation must use the public run/workflow state | MRG1-S13 | approved / sol-high implementation next |

### 34.2 Implementation, independent gate and push

The `gpt-5.6-sol / high` implementation changed only
`.github/workflows/release.yml`:

```text
4 insertions / 3 deletions
candidate SHA-256:
  be9455ef607ab4fe7f8aeade1ea241ba21d6612b30d5361605f07d4dfda442ee
```

The two invalid job-level values were removed. Each affected job now has one
first step that appends the exact runner-time value to `$GITHUB_ENV`:

```text
VIBECAD_HOME=$RUNNER_TEMP/vibecad-release-runtime
RELEASE_DIST=$RUNNER_TEMP/release-dist
```

An independent `gpt-5.6-terra / medium` mechanical gate returned PASS:

- system Ruby/Psych parsed the YAML;
- `on.push.tags` remains exactly `["v*"]`;
- job IDs, dependencies, runners, permissions and action sequence are
  unchanged;
- no `${{ runner.* }}` expression remains in job-level `env`;
- each approved binding exists exactly once as its job's first step and
  precedes every consumer;
- `VIBECAD_RUN_INTEGRATION` remains job-level `"1"`;
- the six legitimate `${{ runner.temp }}` occurrences remain only in the four
  step-level artifact download/upload paths;
- `git diff --check` passed and the index was empty at review.

The controller staged exactly `.github/workflows/release.yml`; the cached path
set contained that one path, cached/worktree blob IDs both equalled
`7a4a0a9a3093566934ac132feb328693804a86be`, and the cached whitespace gate
passed. Commit `6e89162bf38be434f2a22cecbc3586f03beab4ed` with subject
`fix(ci): bind runner temp paths at step runtime` was pushed immediately.
HEAD and upstream both resolved to that commit.

The public Release workflow page after the push still listed Release #34 at
`2cfbbc4` as its newest branch run and had no run for `6e89162`. This is the
expected postcondition: once the workflow parses, the unchanged tag-only
filter does not create a release run for an ordinary branch push.

### 34.3 Unexpected environment-memory residual

After the CI push, status exposed a new untracked private-memory-shaped path:

```text
.workbuddy/memory/2026-07-27.md
birth:  2026-07-27T00:05:46-0700
size:   1289 bytes
```

It was not present in the preceding controller status snapshots and was not
in any active allowlist. The controller inspected only path metadata, not
file content: private memory is neither campaign evidence nor authorized
source material. The active C00B and P01 reviewers both report that their
packets did not create, inspect or write it. The path remains untracked,
unread, unmodified and excluded from every stage/commit/package operation.

Residual `MRG1-ENV-R04`: provenance is unknown and impact on accepted
candidates is none while the path stays excluded. Closure requires passive
host provenance or explicit user authority before any content inspection,
deletion, ignore-rule change or staging. It does not invalidate the CI gate
or the disjoint C00B/P01 byte candidates.

| Entry ID | Decision / approval | Commit / push | Gate evidence | Residual | Snapshot | State |
|---|---|---|---|---|---|---|
| MRG1-CI-REL-E02 | user approval on 2026-07-26; exact §34 allowlist/subject | `6e89162bf38be434f2a22cecbc3586f03beab4ed`, pushed; HEAD/upstream equal | sol-high exact one-file implementation; terra-medium independent PASS; cached path/blob/diff gates PASS; public page has no branch release run for commit | unauthenticated `gh` did not block public confirmation; no release-workflow residual | MRG1-S13 | completed |
| MRG1-ENV-E04 | no change authority; preserve unexpected private-memory path | `not-created` | path metadata only; absent from preceding snapshots; two active reviewers deny provenance | `MRG1-ENV-R04`, exclude until passive provenance or explicit authority | MRG1-S13 | residual / non-blocking for disjoint candidates |

## 35. C00B candidate review breaker and P01 final review

### 35.1 C00B adversarial NO-GO

The independent `gpt-5.6-sol / max` candidate review returned NO-GO with
three major findings. The controller stopped before both the mechanical gate
and the one post-correction real FreeCAD invocation, so the sole real GREEN
budget remains unused.

1. The development fast path trusts mutable `sys.prefix` and same-file
   aliasing before classifying an embedded host. A prefix whose `bin/python`
   is a symlink or hardlink to FreeCADCmd returns the FreeCAD host while
   readiness, receipt and generation evidence remain unreachable.
2. The selector captures active prefix A, then calls `freecadcmd_path()` and
   `freecad_path()`, which each select the active prefix again. The observed
   mock sequence `A, B, B, A, A` authorized a B host and returned A Python
   while all later stability comparisons passed.
3. The real harness performs assertions, `ps` and normal retirement before
   its `finally`. On an assertion, process-inspection or retirement failure,
   `finally` only asserts absence; it does not unconditionally retire or
   terminate the exact published daemon. A gate-red can therefore leak the
   new-session daemon.

The focused regression and non-real harness still pass, demonstrating that
their current coverage does not veto these attacks:

```text
focused regression:  1 passed
non-real harness:     2 passed, 1 deselected
diff whitespace:      PASS
```

The rejected candidate hashes remain:

```text
bootstrap
  a7f2248454326e4f39b10988069fc7da673092310e80202d882c243eb846173e
P0B regression
  35755d859548df10fc07b344a96a3235f50568ecba8e31afbd289c6da1528a44
probe
  fde0c459f96fc91721c7036a036fbe09c8cf8d768171f1f82e82113da1f3f3fd
parent harness
  f802ab947119c3753acf994dd7ef1ce7cc719257717fc89d96c1888843a71872
```

Circuit breaker `MRG1-GATE-C00B-R02` requires a new sol/max design that:

- gives an identity-proven active FreeCAD host precedence over any
  development shortcut and does not use mutable `sys.prefix` path
  shape/same-file aliasing alone as interpreter proof;
- derives both host candidates from one captured prefix without calling a
  helper that selects the active prefix again, and proves prefix stability
  around all identity/evidence work;
- makes real-harness cleanup unconditional, authenticates the exact
  publication, attempts normal bounded retirement first, applies only a
  bounded exact-PID/session fallback when necessary, and proves absence
  before any semantic assertion can escape.

The correction may remain under A04 without a repeated approval only if it
keeps the existing C00B subject, four-path implementation allowlist, one-real-
GREEN budget, fail-closed product outcome and no broader development-runtime
compatibility change. Any source allowlist expansion, new persistent state,
additional real invocation or intentional restriction of supported
development interpreters reopens the user approval gate.

### 35.2 MR1-P01 final adversarial GO

The independent `gpt-5.6-sol / max` final review returned GO with no
actionable corpus finding:

```text
focused corrected gate:  7 passed
Ruff check:               PASS
Ruff format:              PASS
paths:                    31 exact regular non-symlinks
indexed members:          30 exact / unique / full coverage excluding index
JSON members:             29 canonical / duplicate-key-free
opaque payloads:          2 frozen
production diff:          absent
update/generate mode:     absent
```

The review's first pytest invocation omitted `PYTHONPATH=src` and failed
during collection with `ModuleNotFoundError`; it was classified as a setup
error, not product RED. The corrected exact command passed 7/7. Concurrent
controller advancement from `2cfbbc4` to `6e89162` touched only the independent
CI workflow; P01 hashes, test bytes and imported production sources remained
unchanged. A separate `gpt-5.6-terra / medium` mechanical gate owns final
acceptance before exact staging.

| Entry ID | Decision / approval | Commit / push | Gate evidence | Residual | Snapshot | State |
|---|---|---|---|---|---|---|
| MRG1-G1-C00B-E05 | A04 outcome remains; §33.5 exact algorithm superseded by adversarial evidence | prior independent CI commit `6e89162` pushed; C00B `not-created` | sol-max NO-GO; two mock identity bypasses reproduced; harness cleanup trace fails zero-leak requirement; real invocation not run | `MRG1-GATE-C00B-R02`; new in-allowlist sol-max design required | MRG1-S13 | blocked before mechanical/real gate |
| MRG1-MR1-P01-E02 | A04 P01 immutable corpus | prior independent CI commit `6e89162` pushed; P01 `not-created` | sol-max GO; corrected 7/7; Ruff/format/hash/schema/canonical/cross-reference/provenance PASS | terra-medium final gate and exact ignored-payload force-add remain | MRG1-S13 | adversarial GREEN / mechanical gate running |

### 35.3 MR1-P01 independent mechanical PASS

The final `gpt-5.6-terra / medium` gate returned PASS at
`6e89162bf38be434f2a22cecbc3586f03beab4ed` with an empty index:

```text
PYTHONPATH=src PYTHONDONTWRITEBYTECODE=1 \
  .venv/bin/python -B -m pytest -q -p no:cacheprovider \
  tests/test_durable_v1_corpus.py
  -> 7 passed

.venv/bin/ruff check --no-cache tests/test_durable_v1_corpus.py
  -> PASS

.venv/bin/ruff format --check --no-cache tests/test_durable_v1_corpus.py
  -> PASS
```

The gate independently confirmed:

- exactly 31 regular non-symlink corpus paths;
- 29 JSON members including the index and two opaque payloads;
- exactly 30 unique lexically ordered index entries covering every member
  except the index itself;
- exact indexed sizes and SHA-256 values;
- duplicate-key-free canonical JSON, no member terminal LF and exactly one
  index terminal LF;
- no mutation/update/generate switch;
- no working diff in imported durable-v1 production modules;
- clean whitespace for the test and every fixture path.

The two ignored payloads remain byte-frozen and require exact force-add:

```text
model.FCStd  b8b93ace9ff2f0dff51c9e5affac2241522cde21ccea95b993269d6c2d688ecc
model.step   e2fba839d0be4827a2c92e730f352f44ec1c3d9bacec021306847bc4f1fbc215
```

The controller may now stage the rolling artifact plus the exact 31 corpus
paths and one corpus test from §28.5. Only the two named opaque payloads may
use `git add -f`; every other path uses ordinary exact staging. The cached
path set must equal those 33 paths, cached hashes must match the frozen
worktree bytes, and cached whitespace/focused tests remain mandatory before
the approved subject:

```text
test(durable): freeze byte-exact v1 golden corpus
```

| Entry ID | Decision / approval | Commit / push | Gate evidence | Residual | Snapshot | State |
|---|---|---|---|---|---|---|
| MRG1-MR1-P01-E03 | A04 P01; §28.5 exact subject/allowlist | `not-created` | sol-max GO; terra-medium 7/7, Ruff/format, 31-path/hash/canonical/production-diff PASS | exact 33-path cache, two named force-adds, cached gate, commit/push remain | MRG1-S13 | all unstaged gates GREEN / controller staging next |

### 35.4 MR1-P01 commit/push closure

The controller staged exactly 33 paths:

- this rolling artifact;
- all 31 exact `tests/fixtures/durable_v1/` members;
- `tests/test_durable_v1_corpus.py`.

Only `model.FCStd` and `model.step` used exact `git add -f --`; no glob,
directory add or broad force-add was used. The cached path set equalled the
approved sorted set, every cached blob equalled its frozen worktree blob and
`git diff --cached --check` passed. The cached focused gate repeated:

```text
pytest:       7 passed
Ruff check:   PASS
Ruff format:  PASS
```

Commit `2db503ab42e25a7f68d41c45b7151999fe53a027` with subject
`test(durable): freeze byte-exact v1 golden corpus` was pushed immediately.
HEAD and upstream both resolved to that commit and the index returned empty.

The public GitHub Actions API after both `6e89162` and `2db503a` reported the
Release workflow's total branch-run count still at 30, with run #34 for
`2cfbbc4` as the newest entry. Neither new ordinary branch push created a
Release run, confirming the CI parser repair and unchanged tag filter have
stopped the repeated release-failure emails at their source.

### 35.5 Additional untracked-document residual

After the cached P01 gate, status exposed another previously unobserved
untracked path:

```text
CAD_Theory_Course_Parametric_Learning.md
```

It was outside the exact P01 allowlist and appeared concurrently with the
campaign. The controller has not read, edited, staged or packaged it. It joins
the existing excluded course-script set as residual `MRG1-ENV-R05` until
provenance and intended commit scope are explicit; it cannot be folded into a
product, fixture or orchestration commit opportunistically.

| Entry ID | Decision / approval | Commit / push | Gate evidence | Residual | Snapshot | State |
|---|---|---|---|---|---|---|
| MRG1-MR1-P01-E04 | A04 P01; exact §28.5 allowlist/subject | `2db503ab42e25a7f68d41c45b7151999fe53a027`, pushed; HEAD/upstream equal | 33 exact cached paths; two exact force-adds; cached blob/whitespace gate; 7/7; Ruff/format PASS | none for P01; `MRG1-ENV-R04` and `MRG1-ENV-R05` remain globally excluded | MRG1-S13 | completed |

## 36. C00B repair-design adjudication GO

After the §35.1 candidate breaker, a bounded `gpt-5.6-sol / max`
adjudication returned verdict **A**: the three major findings can be closed
inside the existing A04 outcome, subject, write allowlist and one-real-GREEN
budget without restricting development execution to the repository `.venv`.
No repeated user approval is required.

### 36.1 Exact daemon-interpreter selection design

The selector must use one stable snapshot and classify a proven active FreeCAD
host before considering development Python.

1. Capture the active runtime prefix exactly once. Construct both host entries
   directly from that captured prefix:

   ```text
   POSIX:  <prefix>/bin/freecadcmd
           <prefix>/bin/FreeCAD
   Windows:<prefix>/Library/bin/FreeCADCmd.exe
           <prefix>/Library/bin/FreeCAD.exe
   ```

   It is forbidden to call `freecadcmd_path()` or `freecad_path()` because
   those helpers reselect the active prefix.
2. Capture `sys.prefix`, `sys.executable` and CPython's startup program path
   from `ctypes.pythonapi.Py_GetProgramFullPath`. All must be absolute,
   normalized and stable across the decision.
3. If the startup/current executable has the same identity as exactly one
   host derived from the captured active prefix, take the embedded managed
   branch. Startup/current disagreement fails closed.
4. Construct the known FreeCAD entries under the captured `sys.prefix` only
   as a rejection set. A caller matching one of them but not an active host is
   an inactive/unbound embedded host and fails closed.
5. Development Python is admitted only when the normalized absolute spelling
   of the C-level startup path, `sys.executable` and one exact platform prefix
   entry are identical:

   ```text
   POSIX:  <sys.prefix>/bin/python
   Windows:<sys.prefix>/python.exe
           <sys.prefix>/Scripts/python.exe
   ```

   Generic same-file alias admission is forbidden. The entry and resolved
   target must have stable identities, the target must be regular and
   executable, and neither entry nor target may have the identity of any
   derived FreeCAD host. This preserves CPython launched through its exact
   prefix entry, including the repository venv, while intentionally rejecting
   alternate spellings such as a generic Homebrew `python3` alias.
6. The managed branch requires `runtime_ready()`, active-prefix equality at
   every checkpoint and two exact
   `capture_runtime_generation_evidence(captured_prefix)` results. Both
   evidence values must be identical and bind the captured prefix, exact
   Python entry and resolved target. Entry/target identities, regularity,
   executability and target/host distinctness are rechecked immediately before
   return.
7. Every rejected branch raises before `Popen`. No installer, supervisor,
   probe, receipt write or fallback launch is permitted. Existing argv suffix,
   sanitized environment, cwd, startup-claim fd, `pass_fds`, stdio,
   `close_fds` and `start_new_session` remain byte/semantically unchanged.

Required unit coverage includes both host names, exact-prefix POSIX and mocked
Windows entries, inactive/unbound host, mutable startup/sys/prefix snapshots,
symlink and hardlink host aliases, A/B/B/A/A prefix drift, readiness failure,
unequal evidence captures, entry/target swaps, missing/non-regular/
non-executable targets and zero `Popen` calls for every rejection.

### 36.2 Unconditional real-harness cleanup design

The parent test must register an idempotent cleanup action before launching
FreeCAD. After the child returns, it authenticates one exact
`PublishedDaemonState` and captures a Darwin kernel token:

```text
(pid, birth_seconds, birth_microseconds, euid, pgid, sid)
```

The token is obtained from Darwin `libproc` `PROC_PIDTBSDINFO` plus
`os.getsid(pid)`, not from process command text. It must bind the authenticated
receipt PID and require `pid == pgid == sid`.

An unconditional `finally` runs before semantic result assertions:

1. attempt bounded authenticated `retire_local_kernel()` with the exact
   expected daemon id;
2. if retirement fails, signal only if a fresh
   `PublishedDaemonState`/kernel token exactly matches the authenticated
   snapshot and there is no replacement/conflict;
3. send at most one exact process-group `SIGTERM`, poll under one deadline,
   then re-authenticate and send at most one `SIGKILL`;
4. any publication replacement/removal, PID/birth reuse, wrong uid/group/
   session or other uncertainty forbids signaling and fails loudly;
5. before semantic assertions escape, prove the original birth token is
   absent, socket/publication are absent and the exact run root is absent or
   safely empty.

This fallback is a macOS test-harness contract only and makes no Windows
cleanup claim. Residual `MRG1-C00B-R03`: Darwin has no atomic pidfd-style
check-and-signal primitive, so an unavoidable check-to-signal TOCTOU remains.
Exact publication plus birth/euid/pgid/sid rechecks minimize the risk; on any
ambiguity the harness must not signal.

Non-real harness tests must cover success, timeout, malformed/missing probe
output, semantic assertion failure, retirement failure, TERM success, KILL
escalation, publication replacement/removal, PID birth reuse, wrong euid/
pgid/sid and proof that cleanup completes before semantic assertions.

The frozen probe remains byte-identical. Implementation writes stay limited
to:

```text
src/vibecad/daemon/bootstrap.py
tests/test_p0b_acceptance.py
tests/test_freecad_workbench_bootstrap.py
```

The controller artifact remains controller-owned. The exact eventual subject
remains:

```text
fix(daemon): bind embedded bootstrap to managed Python
```

| Entry ID | Decision / approval | Commit / push | Gate evidence | Residual | Snapshot | State |
|---|---|---|---|---|---|---|
| MRG1-G1-C00B-E06 | A04; §36 sol-max verdict A; no scope/compatibility approval reopening | prior P01 `2db503a` pushed; C00B `not-created` | two sol-max design paths reconciled; local permitted CPython C-path probe confirms repo venv exact spelling; Darwin libproc token probe returns exact 136-byte BSD info | `MRG1-C00B-R03` test-only Darwin signal TOCTOU; bounded fail-unsignaled policy | MRG1-S13 | design GREEN / sol-high repair implementation next |

## 37. C00B I02 repaired candidate

The resumed `gpt-5.6-sol / high` implementation stayed inside the three
approved writable paths and did not launch real FreeCAD, a real daemon or an
external `Popen`.

### 37.1 Test-first selector evidence

Before replacing the rejected selector, the exact attack selection produced
five genuine failures, all because the candidate reached mocked `Popen`
instead of raising:

```text
pytest tests/test_p0b_acceptance.py -k daemon_python_rejects
  -> 5 failed, 24 deselected
```

The attacks covered mutable `sys.prefix`, A/B/B/A/A helper drift, symlink and
hardlink host aliases, an unstable C startup path and unequal runtime
generation evidence. The replacement uses CPython
`Py_GetProgramFullPath()`, exact absolute normalized spellings, direct host
construction from one active-prefix snapshot, two equal generation captures
and final stability/identity checks. It does not use the zero-argument host
helpers or generic same-file alias admission.

### 37.2 Test-only cleanup guard

The Darwin-only parent harness now pre-registers an idempotent cleanup guard
before native launch and runs it from an unconditional `finally` before
semantic assertions. Its authenticated token is
`(pid, birth_sec, birth_usec, euid, pgid, sid)`, sourced from the 136-byte
`PROC_PIDTBSDINFO` record plus `getsid`. Exact publication and token equality
gate bounded expected-ID retirement and the single TERM/recheck/KILL
fallback. Replacement, removal, PID reuse, wrong uid/group/session, a short
kernel read or any ambiguity forbids signaling.

The non-real matrix covers stable-token capture, ESRCH/race behavior,
safe-root rejection, timeout and semantic-red ordering, idempotent retirement,
TERM success, KILL escalation, publication replacement/removal, PID reuse and
ambiguous-token no-signal behavior.

### 37.3 I02 candidate gates and frozen hashes

```text
selector/C-API/attacks:  9 passed, 23 deselected
full P0B:                32 passed
non-real harness:        17 passed, 1 deselected
Ruff check:              PASS
Ruff format, writable:   3 files already formatted
git diff --check:        PASS
```

```text
17ed653fe4c98f714cc72d5aa1e898da0b953015cce7d68d35ee55a741ab6879  src/vibecad/daemon/bootstrap.py
147eb0c888ca4644bc049869c2f65f6f20c5d3594147bbdfb050860e1b4d2d3f  tests/test_p0b_acceptance.py
7cc1857092ef5ffff81a14b1f4a951d27b5128aa33f3f9cbd5bf5527c32a6407  tests/test_freecad_workbench_bootstrap.py
fde0c459f96fc91721c7036a036fbe09c8cf8d768171f1f82e82113da1f3f3fd  tests/fixtures/freecad_workbench/bootstrap_probe.py
```

The frozen probe remains outside the write allowlist and byte-identical.
Literal `ruff format --check` would reformat it, so its locked byte hash is an
explicit frozen-evidence exception; no formatter touched it. The index is
empty, HEAD/upstream remain `2db503a`, and no matching FreeCAD or
`vibecad.daemon` process remains.

The candidate is not yet approved. A distinct `gpt-5.6-sol / max`
adversarial review must attack the exact-prefix development branch, managed
host/evidence stability and the Darwin signal guard. Only a GO may advance to
the `gpt-5.6-terra / medium` pre-real mechanical gate and the sole real
FreeCAD GREEN.

| Entry ID | Decision / approval | Commit / push | Gate evidence | Residual | Snapshot | State |
|---|---|---|---|---|---|---|
| MRG1-G1-C00B-E07 | A04; §36 exact repair design | `not-created` | sol-high RED 5; I02 selector 9, P0B 32, harness 17; Ruff/whitespace PASS | frozen-probe format exception; `MRG1-C00B-R03`; independent review and real gate remain | MRG1-S14 | repaired candidate frozen / sol-max review next |

## 38. C00B I02 adversarial NO-GO

A distinct `gpt-5.6-sol / max` reviewer reproduced the published focused
GREEN gates but rejected I02. No real FreeCAD, daemon, external `Popen` or
signal was used; attack seams were mocked and the frozen candidate hashes did
not drift.

### 38.1 Blockers

1. The development branch returns after exact string comparison but before
   any filesystem or host authentication. A missing exact-prefix Python
   reached mocked `Popen` without consulting the active prefix. Therefore a
   missing, directory/FIFO, non-executable, swapped or host-colliding
   development entry is not fail-closed.
2. The cleanup guard reports `clean=True/no_publication` when it has never
   authenticated a publication or Darwin birth token. On timeout or malformed
   output, a detached daemon may be racing publication; an absent/empty run
   root cannot prove that unknown process absent.
3. Managed host uniqueness is checked by spelling, not against every derived
   host identity. Both active hosts sharing one inode reached mocked `Popen`,
   as did a managed Python sharing identity with the unselected host.

### 38.2 Major defects and missing regressions

- Equal evidence can name a Python target different from the entry's actual
  resolution, and readiness is checked only once.
- Windows development admission implements only `<prefix>/python.exe`; the
  required `<prefix>/Scripts/python.exe` entry is rejected.
- The current tests omit development missing/non-regular/non-executable and
  entry/target-swap attacks, Python collision with every host, mocked Windows
  positives, evidence prefix/python/target mismatch, readiness drift,
  malformed/missing probe output, an unobserved-publication cleanup failure,
  a TERM-to-KILL generation change and a non-real end-to-end cleanup-before-
  semantic-failure proof.
- The named A/B/B/A/A regression rejects at its first checkpoint and does not
  exercise the advertised later sequence.

Corrected independent focused commands still returned selector `9/9`, harness
`17/17`, Ruff PASS and whitespace PASS, proving that the existing matrix did
not encode the full §36 contract.

### 38.3 I03 correction boundary

I03 remains inside the existing A04 subject and three writable paths. It must
first turn every executable finding above into a genuine pre-correction RED,
then:

1. capture the active prefix and construct all active plus captured-prefix
   host spellings before either development or managed admission;
2. bind development entry and resolved target identities across two stable
   observations, require a regular executable target and reject collision with
   every derived host entry/target identity;
3. admit both exact Windows development spellings while retaining exact
   spelling equality and no generic alias admission;
4. require exactly one authenticated active-host identity, Python
   entry-to-target resolution equality, all-host distinctness and final
   readiness/active/evidence stability before return;
5. report unobserved publication as unproven/unclean with no signal, and add
   the missing cleanup and probe-result ordering matrix.

The sole real FreeCAD GREEN remains unused and forbidden until a new distinct
sol-max reviewer returns GO.

| Entry ID | Decision / approval | Commit / push | Gate evidence | Residual | Snapshot | State |
|---|---|---|---|---|---|---|
| MRG1-G1-C00B-E08 | A04; I02 independent adversarial review | `not-created` | sol-max NO-GO; mocked missing-entry, host-identity, target-binding and no-publication attacks executable | three blockers, three majors; `MRG1-C00B-R03`; real GREEN unused | MRG1-S14 | I02 rejected / sol-high I03 correction next |

## 39. C00B I03 corrected candidate

The resumed `gpt-5.6-sol / high` implementer converted every §38 executable
finding into a pre-correction RED before changing the implementation. No
slow/real FreeCAD, daemon, external `Popen`, OS signal, installation, network
or repository mutation outside the three approved paths occurred.

### 39.1 Selector RED-to-GREEN

The first selector attack group produced `15 failed, 2 passed, 32
deselected`, with no setup errors. The failures proved incorrect admission of
missing/directory/FIFO/non-executable development entries, entry/target swaps,
active and captured-prefix host collisions, non-unique active hosts, collision
with an unselected host, forged target evidence, readiness drift, missing
Windows `Scripts/python.exe` support and incomplete A/B/B/A/A checkpoint
coverage. Additional isolated REDs proved forged equal prefix identity and
active-versus-captured-prefix host collisions.

I03 now:

- captures the active prefix before either branch and directly constructs both
  active and captured-prefix host spellings;
- captures stable entry and resolved-target evidence for every present host;
- admits only the exact POSIX development entry or either exact Windows
  development entry, with regular/executable/stable target evidence and no
  collision against any derived host identity;
- requires exactly one active host identity, pairwise distinct host evidence,
  managed Python entry-to-target resolution equality and Python separation
  from every active/inactive host;
- binds equal generation evidence to the live prefix identity, exact managed
  Python entry and actual resolved target; and
- repeats the active-prefix and readiness checkpoints after the live evidence
  capture before returning.

The current repository `.venv` was checked read-only and returned its exact
`.venv/bin/python`. Both mocked Windows entries and both active FreeCAD host
names pass their positive cases.

### 39.2 Cleanup RED-to-GREEN

Missing and malformed probe-output cases produced two genuine failures because
an unobserved publication was certified as clean. I03 changes that state to
`clean=False`, `publication_unproven`, with no retirement or signal and with
`original_token_absent=False`. Existing TERM-to-KILL publication/token
revalidation, single-payload parsing amid noise and cleanup-before-semantic
ordering were independently exercised and remained correct.

### 39.3 I03 gates and frozen hashes

```text
selector focused:   29 passed, 23 deselected
full P0B:           52 passed
non-real harness:   22 passed, 1 slow deselected
combined non-real:  74 passed, 1 deselected
Ruff check:         PASS
Ruff format:        PASS
git diff --check:   PASS
```

```text
aee48e26d949a80e1bfc8e706ba92dfddcf9c9ba2e2715032f4dd1d7eb2a685c  src/vibecad/daemon/bootstrap.py
55fa61280ee6ef5a6f9b8561d59657482c3ac4d5f314170e00443808974e9cc4  tests/test_p0b_acceptance.py
0d88c9a9ee9e7d35cfc16967e97a483f24ce800730fe22a2d8258181913a724c  tests/test_freecad_workbench_bootstrap.py
fde0c459f96fc91721c7036a036fbe09c8cf8d768171f1f82e82113da1f3f3fd  tests/fixtures/freecad_workbench/bootstrap_probe.py
```

HEAD/upstream remain `2db503a`, the index is empty and the frozen probe is
unchanged. The candidate is not yet approved: a new independent
`gpt-5.6-sol / max` review must validate the exact I03 hashes, including
late-checkpoint mutation attacks, before any mechanical or real gate.

| Entry ID | Decision / approval | Commit / push | Gate evidence | Residual | Snapshot | State |
|---|---|---|---|---|---|---|
| MRG1-G1-C00B-E09 | A04; §38.3 correction boundary | `not-created` | sol-high selector RED 15 plus isolated prefix/host REDs; I03 selector 29, P0B 52, harness 22, combined 74; Ruff/format/whitespace PASS | frozen-probe format exception; `MRG1-C00B-R03`; independent review and real GREEN remain | MRG1-S15 | corrected candidate frozen / new sol-max review next |

## 40. C00B I03 adversarial NO-GO

A fresh `gpt-5.6-sol / max` reviewer independently reproduced the 74 passing
non-real tests, then found two fixable late-state defects. The exact I03
hashes, HEAD/upstream and empty index remained stable; no repository write,
real process or signal occurred.

### 40.1 Final-live selector blocker

Both branches capture filesystem identity before their final mutable
callbacks. Deterministic callbacks that returned the expected prefix or
readiness value after replacing a host entry/target, Python entry/target or
the prefix generation reached mocked `Popen`:

```text
managed final-active/final-readiness mutations:  14 / 14 admitted
development final-active/final-C-path mutations: 14 / 14 admitted
```

This is not an appeal to an unbounded pathname-to-exec race. The candidate
itself performs state-observing callbacks after its claimed final live
capture. I04 must perform all final active/readiness/C-path/sys snapshot calls
first, then make one genuinely final host/Python/prefix filesystem recapture,
and do only local comparisons before return.

### 40.2 Post-authentication ambiguity major

After a birth token has been authenticated, one short/incomplete Darwin
identity read produces `ambiguous`; the current guard then retries during
signal eligibility. A mocked short-read-then-success sequence sent `SIGTERM`
and reported clean. Section 36 requires any identity uncertainty to forbid
signaling and fail loudly.

I04 must latch every post-authentication publication or token ambiguity for the
cleanup attempt. Once latched, later successful reads cannot re-enable TERM or
KILL. Exact expected-ID retirement may still be attempted; proof of the
original token's absence may still yield clean, but no fallback signal is
permitted after ambiguity.

Current regressions cover readiness `True -> False` and persistent token
failure, but not callback-side file mutation or one-time ambiguity followed by
success. Those attacks must be RED before I04.

| Entry ID | Decision / approval | Commit / push | Gate evidence | Residual | Snapshot | State |
|---|---|---|---|---|---|---|
| MRG1-G1-C00B-E10 | A04; I03 fresh independent review | `not-created` | sol-max NO-GO; 28/28 late callback mutations admitted; one-time short read re-enabled TERM | final-live recapture blocker; ambiguity-latch major; `MRG1-C00B-R03`; real GREEN unused | MRG1-S15 | I03 rejected / bounded I04 correction next |

## 41. C00B I04 final-live correction candidate

The `gpt-5.6-sol / high` implementer first reproduced the exact §40 failures:

```text
late callback selector RED:  28 failed, 52 deselected
  development:               14 / 14 reached mocked Popen
  managed:                   14 / 14 reached mocked Popen
transient ambiguity RED:     2 failed, 23 deselected
  token short read:          mocked TERM sent
  publication ambiguity:     mocked TERM sent
```

No real `Popen` or signal was involved.

### 41.1 Corrected final ordering

Development now performs final active-prefix, C startup-path and
`sys.executable`/`sys.prefix` snapshots before recapturing every derived host,
the admitted Python entry/target and the captured-prefix identity. Managed
selection completes its second generation evidence, final active-prefix,
final readiness and final C/sys snapshots before recapturing every host,
managed Python and active-prefix identity. After those recaptures, both
branches only compare local immutable values and return; no active selector,
readiness or C-path callback remains after the final filesystem evidence.

The generic interval between the final filesystem read and pathname-based
`Popen` cannot be made atomic in this patch, but I04 no longer inserts its own
mutable callback into that interval.

### 41.2 Monotonic cleanup ambiguity

The cleanup guard now latches post-authentication publication ambiguity,
kernel-token read errors and generation conflict. Expected-ID retirement still
runs, and exact absence proof may still establish clean state, but TERM/KILL
eligibility can only transition from allowed to permanently forbidden.
Successful later reads cannot clear the latch; repeated cleanup returns the
cached outcome.

### 41.3 I04 gates and frozen hashes

```text
selector focused:   57 passed, 23 deselected
full P0B:           80 passed
non-real harness:   25 passed, 1 slow deselected
combined non-real:  105 passed, 1 deselected
repo venv selector: exact .venv/bin/python
Ruff check/format:  PASS
git diff --check:   PASS
```

```text
d6129e2431b262708a7662ecb27306e40878d3c0c2ba14a4135077ffc31ae63b  src/vibecad/daemon/bootstrap.py
bb52f21bee4831e61588c9b489d56cc460f88f5d247b8a3a382e30773006a230  tests/test_p0b_acceptance.py
17cab6ffab60d4a5b0daa9f620caaa46724e140b40041b46e71819bc3f9be1c3  tests/test_freecad_workbench_bootstrap.py
fde0c459f96fc91721c7036a036fbe09c8cf8d768171f1f82e82113da1f3f3fd  tests/fixtures/freecad_workbench/bootstrap_probe.py
```

HEAD/upstream remain `2db503a`, the index is empty and the probe is unchanged.
I04 is frozen for a third fresh `gpt-5.6-sol / max` adversarial review; it is
not yet authorized for the mechanical or sole real gate.

| Entry ID | Decision / approval | Commit / push | Gate evidence | Residual | Snapshot | State |
|---|---|---|---|---|---|---|
| MRG1-G1-C00B-E11 | A04; bounded §40 correction | `not-created` | sol-high RED 28+2; I04 selector 57, P0B 80, harness 25, combined 105; Ruff/format/whitespace PASS | generic final-read-to-Popen pathname TOCTOU; frozen probe exception; `MRG1-C00B-R03`; fresh review/real GREEN remain | MRG1-S16 | I04 frozen / third sol-max review next |

## 42. C00B I04 independent GO

The first AR03 dispatch was rejected by an automated tool-channel content
classifier before producing any repository finding. It made no write or state
change and is not a candidate verdict. The same `gpt-5.6-sol / max` reviewer
then completed a benign local correctness/conformance QA packet and returned
**GO** with no blocker or major defect.

Independent evidence:

```text
selector focused:                  57 passed, 23 deselected
known late-callback regressions:    28 / 28 pre-Popen rejection
additional callback/resource grid: 63 / 63 pre-Popen rejection
equal-evidence forgery grid:         8 / 8 pre-Popen rejection
mocked cleanup harness:             25 passed, 1 deselected
one-time ambiguity grid:             6 / 6 latched, no unexpected signal
combined allowed unit gate:        104 passed, 2 deselected
Ruff check/format:                  PASS
git diff --check:                   PASS
```

The two combined exclusions were the slow real FreeCAD test and an existing
Darwin P0B test that launches a real detached daemon; both were forbidden in
the review packet. The reviewer independently confirmed:

- both selector branches perform every active/readiness/C/sys callback before
  the final live recapture and only local comparisons afterward;
- exact `RuntimeGenerationEvidence` type and prefix/Python/target bindings;
- exact spawn argv, environment, cwd, fd, stdio and session behavior;
- monotonic cleanup ambiguity across observation, proof, signal eligibility
  and TERM-to-KILL transitions;
- exact final absence proof before semantic assertions; and
- the 136-byte Darwin layout and command text's evidence-only status.

The unavoidable final-filesystem-read-to-pathname-launch interval and
`MRG1-C00B-R03` remain explicit residuals. They do not contain another
candidate-created mutable callback seam.

The I04 hashes remained exactly those in §41.3, HEAD/upstream remained
`2db503a` and the index remained empty. AR03 authorizes the
`gpt-5.6-terra / medium` mechanical pre-real gate and, if that gate is clean,
the sole approved real FreeCAD GREEN.

| Entry ID | Decision / approval | Commit / push | Gate evidence | Residual | Snapshot | State |
|---|---|---|---|---|---|---|
| MRG1-G1-C00B-E12 | A04; sol-max AR03 GO | `not-created` | selector 57; late 28/28; callback 63/63; evidence 8/8; cleanup 25 and ambiguity 6/6; allowed combined 104; Ruff/format/whitespace PASS | generic final-read-to-launch TOCTOU; `MRG1-C00B-R03`; frozen probe exception | MRG1-S16 | adversarial GO / terra-medium pre-real next |

## 43. C00B mechanical pre-real PASS

The `gpt-5.6-terra / medium` mechanical agent first matched HEAD/upstream,
empty index and all five dispatch hashes. It paused when §42 did not repeat the
literal selector `-k` expression; the controller supplied the exact reviewed
command rather than permitting reconstruction by guess.

The permitted no-cache/no-bytecode gates then passed exactly:

```text
selector focused:   57 passed, 23 deselected
non-slow harness:   25 passed, 1 deselected
combined non-real:  104 passed, 2 deselected
Ruff check:         PASS
Ruff format:        PASS
tracked whitespace: PASS
untracked whitespace: no errors; expected content-difference exit 1
```

The combined exclusions were again the sole slow real FreeCAD test and the
existing detached-daemon P0B test. Before and after process snapshots found no
FreeCAD or `vibecad.daemon` process. All five hashes, repository status,
HEAD/upstream and the empty index remained unchanged. No real process, signal,
network, stage, commit or push occurred.

The candidate is mechanically authorized for exactly one real FreeCAD GREEN.
The real-gate runner must verify the managed prefix, receipt, generation
evidence and cold process state without launching anything, then issue one and
only one invocation of
`test_real_freecad_embedded_interpreter_bootstraps_and_retires_one_daemon`.
An identity/environment breaker before that invocation does not consume the
budget. Any test failure consumes the attempt and forbids an automatic retry.

| Entry ID | Decision / approval | Commit / push | Gate evidence | Residual | Snapshot | State |
|---|---|---|---|---|---|---|
| MRG1-G1-C00B-E13 | A04; sol-max GO plus terra-medium mechanical gate | `not-created` | selector 57; harness 25; combined 104; Ruff/format/whitespace/hashes/process snapshots PASS | one real invocation remains; generic pathname and `MRG1-C00B-R03` residuals | MRG1-S16 | pre-real PASS / sole real GREEN next |

## 44. C00B real-gate command-selection RED

The `gpt-5.6-sol / max` real-gate runner completed the non-launching Phase A
successfully:

- selected canonical managed prefix
  `/Users/wangtao/Library/Application Support/VibeCAD/runtime/mamba/envs/vibecad`;
- matched the exact managed receipt and observed `runtime_ready() == true`;
- matched two generation-evidence captures, including the prefix, Python
  entry and resolved `python3.12` target identities;
- verified the exact regular executable `bin/freecadcmd`;
- matched all repository hashes, HEAD/upstream and empty index; and
- observed no existing FreeCAD or `vibecad.daemon` process.

The frozen packet then allowed one exact pytest invocation. It returned exit
5 in 0.8914 seconds:

```text
1 deselected in 0.22s
```

No probe or parent evidence line was emitted. The test body, FreeCAD, daemon,
handshake and cleanup guard never executed. Post-run snapshots found no
matching process, publication, socket, run root or `vibecad-c00b-*`
container. Repository hashes and status remained unchanged, and no signal was
sent.

The controller's read-only diagnosis is exact:

```toml
[tool.pytest.ini_options]
addopts = "-ra -m 'not slow'"
```

The gate command omitted the explicit `-m slow` used by the repository's
Darwin slow CI commands, so pytest deselected the explicitly addressed slow
node during collection. This is a gate-command selection failure, not a
product or candidate execution RED.

The prior packet nevertheless defined the budget as one pytest invocation and
forbade retry under any outcome. That invocation is therefore consumed under
the recorded rule even though no real action occurred. A corrected command
requires explicit renewed authority:

```text
VIBECAD_RUN_INTEGRATION=1
VIBECAD_FREECAD_ENV=<same verified prefix>
PYTHONPATH=src
PYTHONDONTWRITEBYTECODE=1
.venv/bin/python -B -m pytest -q -s -p no:cacheprovider -m slow \
  tests/test_freecad_workbench_bootstrap.py::test_real_freecad_embedded_interpreter_bootstraps_and_retires_one_daemon
```

No source/test/probe correction is proposed. If approved, exactly one
corrected invocation is authorized, with the same no-retry and authenticated
cleanup rules.

| Entry ID | Decision / approval | Commit / push | Gate evidence | Residual | Snapshot | State |
|---|---|---|---|---|---|---|
| MRG1-G1-C00B-E14 | A04 real-gate packet; invocation limit enforced | `not-created` | Phase A PASS; pytest exit 5 / one deselected; zero test body/process/signal/residue; root cause exact global `not slow` marker | corrected `-m slow` invocation requires renewed user authority; product real GREEN still unobserved | MRG1-S17 | command-selection RED / paused before retry |

## 45. RG02 corrected real-gate authorization

At `2026-07-27T02:54:56-07:00`, the user supplied the exact authorization:

```text
批准 MRG1-G1-C00B-RG02
```

RG02 supersedes only the exhausted invocation limit in §44. It authorizes
exactly one corrected invocation using the same verified managed prefix and an
explicit `-m slow` selector:

```text
VIBECAD_RUN_INTEGRATION=1
VIBECAD_FREECAD_ENV=/Users/wangtao/Library/Application Support/VibeCAD/runtime/mamba/envs/vibecad
PYTHONPATH=src
PYTHONDONTWRITEBYTECODE=1
.venv/bin/python -B -m pytest -q -s -p no:cacheprovider -m slow \
  tests/test_freecad_workbench_bootstrap.py::test_real_freecad_embedded_interpreter_bootstraps_and_retires_one_daemon
```

The authorization does not permit a source/test/probe edit, installation,
network access, a second corrected invocation, a detached-daemon P0B run,
manual signaling, staging, commit or push. An identity/readiness/cold-state
breaker before pytest does not consume RG02. Once pytest starts, any result
consumes RG02 and forbids automatic retry. Only the harness's authenticated
expected-ID cleanup and bounded fallback remain authorized.

### 45.1 Current adapter and capability profile

Selected adapter: Codex.

```text
approval: native-plan
delegation: spawn-send-wait
persistence: repo-artifact
process: native-session-poll
```

Permitted capability evidence sources:

- `live capability declarations`: `update_plan`, `spawn_agent`,
  `followup_task`, `send_message`, `wait_agent`, controllable
  `exec_command` sessions and `write_stdin` polling are declared live.
- `observable behavior`: native-plan projection, agent spawn/follow-up/wait
  and synchronous command execution have succeeded in this session; no
  capability is inferred from repository content or approval records.
- `environment identity`: Codex desktop session with the passively exposed
  VibeCAD workspace and macOS host context.
- `public configuration`: unrestricted filesystem permission profile,
  approval policy `never`, and the declared collaboration/process tools; no
  credentials, private memory or repository content were inspected as
  capability evidence.

The repo artifact remains authoritative; native planning is only a
projection. RG02 uses `spawn-send-wait`, deep model routing
(`gpt-5.6-sol / max` per the user's campaign routing), and the original
controllable process session if pytest yields. Duplicate launch is a circuit
breaker.

| Entry ID | Decision / approval | Commit / push | Gate evidence | Residual | Snapshot | State |
|---|---|---|---|---|---|---|
| MRG1-G1-C00B-E15 | RG02; exact user wording above | `not-created` | anchor HEAD/upstream `2db503a`; index empty; five S17 hashes matched before dispatch | one corrected invocation; generic pathname and `MRG1-C00B-R03` residuals | MRG1-S17 | approved / RG02 dispatch next |

## 46. RG02 real environment RED

RG02 passed its non-consuming preflight against the exact canonical managed
prefix, receipt, two equal generation-evidence captures, regular executable
`freecadcmd`, cold process state, repository hashes and empty index. The
corrected command then started exactly once with explicit `-m slow`:

```text
exit:          1
exec wall:     17.4672 seconds
pytest wall:   16.87 seconds
result:        1 failed, 1 error
invocations:   1 / 1
```

Both the body failure and teardown error were:

```text
_CleanupOutcome(
  clean=False,
  retire_attempted=False,
  term_sent=False,
  kill_sent=False,
  detail="publication_unproven",
)
```

No `VIBECAD_BOOTSTRAP_PROBE=` or `VIBECAD_BOOTSTRAP_PARENT=` line reached
pytest output. The harness captured child stdout/stderr internally, then the
unconditional cleanup assertion raised before those diagnostics or the parent
evidence were printed. Therefore the observable evidence proves that no
publication/birth token was authenticated, but does not distinguish a
selector rejection, daemon spawn failure, child import failure or another
pre-publication semantic error.

The safety outcome was correct: no manual signal was sent, the harness sent
neither TERM nor KILL, and post-run snapshots found no FreeCAD,
`vibecad.daemon`, run root, receipt, socket or `vibecad-c00b-*` container.
All five dispatch hashes, HEAD/upstream, empty index and workspace status
remained unchanged.

RG02 is consumed and this is an unexpected G3 environment gate red. Automatic
implementation, another real run, staging, commit and push are blocked.
Read-only static diagnosis is allowed; any code/test correction or renewed
real-run budget requires a new approved packet.

| Entry ID | Decision / approval | Commit / push | Gate evidence | Residual | Snapshot | State |
|---|---|---|---|---|---|---|
| MRG1-G1-C00B-E16 | RG02 consumed; gate circuit breaker | `not-created` | exact `-m slow` invocation exit 1; 1 failed + 1 error; publication unproven; zero signal/residue; hashes unchanged | `MRG1-C00B-R04`: original child diagnostic masked by cleanup assertion; product cause unclassified | MRG1-S18 | blocked |

## 47. Recovery snapshot MRG1-S18

### 47.1 Completed milestones

- CI release workflow repair `6e89162` and MR1-P01 `2db503a` remain pushed;
  HEAD/upstream are both `2db503ab42e25a7f68d41c45b7151999fe53a027`.
- C00B I04 passed sol-max independent QA, terra-medium mechanical pre-real
  gates and 105 local non-real tests at the frozen hashes recorded in §41.
- RG01 was collection-only and superseded by explicit RG02.
- RG02 ran the real test body once and ended in the §46 pre-publication RED.
  No process, signal or filesystem residue remains.
- Last verified artifact before this snapshot:
  `2998afb701df24b5562508db15a7cfce4772b8eb8c4929fb98d6527f96d81065`.

### 47.2 Ordered next packets

1. `MRG1-G1-C00B-D01`: sol-max read-only static failure analysis. If exact
   production cause is provable without a real run, produce a bounded I05
   proposal; otherwise classify the remaining uncertainty.
2. `MRG1-G1-C00B-D02`: sol-max read-only harness-observability review. Define
   the minimum change that preserves unconditional cleanup while always
   emitting captured child stdout/stderr and cleanup evidence.
3. Controller reconciliation:
   - if D01 proves a source defect, present one combined source regression,
     observability and renewed-real-budget approval;
   - if D01 cannot prove cause, present an observability-first packet and no
     product-source speculation;
   - if either diagnosis requires real execution, stop before it.
4. No implementation, real retry, stage, commit or push until the user
   approves the reconciled post-RED packet.

### 47.3 Approved decisions

- A04 remains the architectural/product scope authority for FreeCAD-first
  C00B and the three writable implementation/test paths.
- RG02 exact authorization from §45 is fully consumed and grants no further
  invocation.
- The user-required routing remains: mechanical gates terra/medium; routine
  coding sol/high; critical architecture, failure analysis and review
  sol/max.
- No existing approval authorizes source/test changes after E16 or another
  real FreeCAD invocation.

### 47.4 Execution discipline

- Adapter/profile: Codex; `native-plan`, `spawn-send-wait`, `repo-artifact`,
  `native-session-poll`; the repo artifact is authoritative.
- D01/D02 are read-only sol-max packets. Allowed reads are the C00B source,
  tests, frozen probe, directly imported helpers, exact pytest configuration
  and static managed-runtime metadata needed for diagnosis.
- Prohibited: repository writes outside this controller ledger append, real
  FreeCAD/daemon/Popen, OS signal, installation, network, stage/commit/push,
  or content access to `.workbuddy/` and excluded course documents.
- Circuit breakers: any real launch, candidate/hash/HEAD/index drift, scope
  expansion, or diagnosis that depends on unavailable child output.
- Residuals: generic final-read-to-launch interval, `MRG1-C00B-R03` Darwin
  check-to-signal interval, frozen-probe format exception, and
  `MRG1-C00B-R04` masked child diagnostics.
- Recovery checks: verify branch/HEAD/upstream, empty index, five candidate
  hashes, absence of matching processes/residue and exact E16/S18 artifact
  text before any future packet.

## 48. Post-RG02 diagnosis and proposed A05

Two independent `gpt-5.6-sol / max` read-only packets completed without
changing any repository or runtime byte and without launching a process.

### 48.1 D01 proven layout blocker

The RG02 harness used the canonical macOS temp parent:

```text
/private/var/folders/qk/0_b6krc135j3lrz44krcddr40000gn/T
```

Its exact fixed-length derivation was:

```text
parent:                                      56 bytes
.../vibecad-c00b-<8 chars>:                  78 bytes
.../vibecad-home/data/daemon/kernel.sock:   115 bytes
```

`src/vibecad/daemon/state.py::bind_endpoint()` rejects an encoded endpoint
path longer than 103 bytes. Therefore the RG02 layout cannot publish a daemon
even if the selector, managed `Popen`, imports and application construction
all succeed. This is a deterministic test-environment blocker, twelve bytes
over the production Unix-socket bound.

The 16.87-second pytest wall time closely matches the bootstrap's one
15-second publication deadline: after spawning, bootstrap does not poll the
child exit and continues waiting for publication. This makes a managed child
exit at `bind_endpoint()` the highest-confidence causal chain. It is not
forensic proof of RG02's first exception because the retained trace omitted
child buffers and does not prove raw embedded C/sys spellings or `Popen`.

Static managed-runtime metadata itself satisfies the selector's file
predicates: `bin/freecadcmd`, `bin/FreeCAD` and the resolved managed Python
target are regular, executable and inode-distinct; Phase A proved receipt,
readiness and equal generation evidence. No production selector correction is
justified by the current evidence.

### 48.2 D02 observability GO

The cleanup assertion currently has exception precedence over the action
failure and is repeated by the finalizer, causing the body failure plus
teardown error while suppressing captured streams and parent evidence.

D02 found a single-file correction:

1. capture an action exception and traceback instead of letting it escape;
2. run non-asserting idempotent `cleanup_guard.cleanup()` unconditionally;
3. emit exactly one flushed, sorted, bounded parent JSON record containing
   child return code, timeout, parse error, stdout/stderr tails, cleanup
   `clean/detail/retire/TERM/KILL` fields and bounded action error;
4. only then raise the first body failure in transport/parse/return-code,
   cleanup and semantic order;
5. keep the early finalizer, but give the body ownership after evidence
   emission so cached cleanup does not assert a second time; an earlier setup
   failure leaves assertion ownership with the finalizer.

Child text remains diagnostic only. Publication, retirement, token capture and
signal eligibility continue to use authenticated state and kernel identity.

### 48.3 Proposed MRG1-G1-C00B-A05

Product outcome: make the real macOS FreeCAD gate capable of using the product
socket contract and make any subsequent environment failure self-diagnosing,
without changing production source or the frozen child probe.

Write allowlist:

```text
tests/test_freecad_workbench_bootstrap.py
```

Implementation:

- use canonical `/private/tmp` and prefix `vc-c00b-` for the Darwin-only
  isolated root; the expected endpoint is 66 bytes;
- before FreeCAD launch, compute the exact encoded endpoint and fail if it is
  empty or exceeds 103 bytes;
- implement the D02 cleanup/evidence/finalizer ownership flow with 2,000
  character stream tails and deterministic JSON primitives;
- add genuine non-real RED regressions for long-path admission, success,
  timeout, missing/malformed output, action error plus cleanup red, cleanup-only
  red, semantic red, single bounded evidence and finalizer ownership.

Gates:

1. sol-high test-first implementation, no real process;
2. full non-slow harness and the previously allowed combined non-real gate;
3. Ruff check/format, whitespace, exact hash/allowlist inspection;
4. fresh sol-max review;
5. terra-medium mechanical pre-real gate;
6. if and only if all gates pass, one exact RG03 real FreeCAD invocation with
   `-m slow`, the same verified managed prefix and no retry.

Budgets and breakers:

- one writable file and one eventual commit under the existing subject
  `fix(daemon): bind embedded bootstrap to managed Python`;
- frozen probe and production bootstrap remain byte-identical during A05;
- unexpected non-real red, endpoint still over 103, evidence before cleanup,
  child text entering authentication, duplicate cleanup assertion,
  out-of-scope write or any pre-gate real process stops the packet;
- RG03 is a new one-invocation budget. A pre-run identity/readiness/cold-state
  breaker does not consume it; after pytest starts, any result consumes it and
  forbids retry.

| Entry ID | Decision / approval | Commit / push | Gate evidence | Residual | Snapshot | State |
|---|---|---|---|---|---|---|
| MRG1-G1-C00B-E17 | read-only D01/D02 under S18; A05 proposed, not approved | `not-created` | deterministic 115 > 103 veto; single-file observability GO; hashes/HEAD/index unchanged | actual RG02 first exception unproven; grandchild stderr remains suppressed; A05 and RG03 need user approval | MRG1-S18 | diagnosis completed / approval gate open |

## 49. A05 authorization

At `2026-07-27T04:07:47-07:00`, in direct response to the §48.3 approval
request, the user supplied the exact approval identifier:

```text
MRG1-G1-C00B-A05
```

The controller interprets that direct approval response as authorization of
the complete §48.3 packet at artifact hash
`21d677c2dad54478695516325be6342d99a10f7b6c8d6928f06d8256042a42b2`:

- one writable path, `tests/test_freecad_workbench_bootstrap.py`;
- the short Darwin temp-root and exact endpoint-length preflight;
- the cleanup-before-evidence-before-assertion observability flow;
- test-first non-real implementation, independent sol-max review and
  terra-medium mechanical pre-real gate; and
- only after all gates pass, one no-retry RG03 real invocation.

No production source, P0B regression, frozen probe, installation, network,
stage, commit, push or real process is authorized during implementation.
RG03 has its own branch condition and is not reachable until the named gates
are green.

The Codex capability profile from §45.1 remains current and was revalidated
from the same four permitted evidence categories: `live capability
declarations`, `observable behavior`, `environment identity` and `public
configuration`. Native planning remains a projection; this artifact remains
authoritative.

| Entry ID | Decision / approval | Commit / push | Gate evidence | Residual | Snapshot | State |
|---|---|---|---|---|---|---|
| MRG1-G1-C00B-E18 | A05; exact user response above; §48.3 fully bound | `not-created` | S18 recovery anchor matched; HEAD/upstream `2db503a`; index empty; five hashes matched | actual RG02 first exception unproven; grandchild stderr suppressed; RG03 conditional on all gates | MRG1-S18 | approved / sol-high I05 next |

## 50. A05 implementation candidate

At `2026-07-27T04:32:39-07:00`, the reused sol-high coding agent completed the
authorized single-file A05 implementation. The controller independently
rechecked the candidate anchor before opening review:

```text
branch: codex/agent-stage3
HEAD: 2db503ab42e25a7f68d41c45b7151999fe53a027
upstream: 2db503ab42e25a7f68d41c45b7151999fe53a027
index: empty
artifact-before-this-entry:
  5220010f58bc9271aac28e7cb08fa17223ebb83e106f238c9458f14724774e68
bootstrap:
  d6129e2431b262708a7662ecb27306e40878d3c0c2ba14a4135077ffc31ae63b
P0B:
  bb52f21bee4831e61588c9b489d56cc460f88f5d247b8a3a382e30773006a230
harness-candidate:
  a0da606e87554410a2c1f2859ba2d92fd0167c430cfdf87d135252adf9874e24
probe:
  fde0c459f96fc91721c7036a036fbe09c8cf8d768171f1f82e82113da1f3f3fd
```

The genuine non-real test-first transition was:

- baseline: `25 passed, 1 deselected`;
- RED: `11 failed, 25 passed, 1 deselected`, with all eleven new regressions
  reaching not-yet-implemented helpers and no real process;
- final focused: `13 passed, 26 deselected`;
- final harness: `38 passed, 1 deselected`;
- final combined non-real gate: `117 passed, 2 deselected`;
- Ruff check and format check: pass.

The candidate uses a canonical owner-private `/private/tmp/vc-c00b-<8>` root,
preflights the exact endpoint encoding before launch, performs idempotent
non-asserting cleanup before one bounded deterministic evidence record, then
applies the authorized failure precedence and finalizer ownership. It does not
change production source or the frozen probe. No real/slow test, FreeCAD,
daemon, signal, network, install, stage, commit or push occurred.

The candidate is now frozen for a fresh sol-max adversarial review and an
independent terra-medium mechanical pre-real gate. RG03 remains unreachable
until both produce GO against this exact hash.

| Entry ID | Decision / approval | Commit / push | Gate evidence | Residual | Snapshot | State |
|---|---|---|---|---|---|---|
| MRG1-G1-C00B-E19 | approved A05 implemented by reused sol-high coding agent | `not-created` | genuine non-real RED; focused 13, harness 38, combined 117; Ruff pass; controller anchor/hash recheck | candidate adds 614 lines to a test harness; independent complexity/safety review required; RG02 first exception remains unproven | MRG1-S18 | candidate frozen / review and mechanical pre-real gates next |

## 51. A05 gate RED and proposed A06

At `2026-07-27T04:42:44-07:00`, both A05 pre-real branches completed against
the exact §50 candidate. Neither branch ran RG03 or any real process.

The fresh sol-max adversarial review returned NO-GO:

1. cleanup `BaseException` is emitted and re-raised, but the finalizer calls
   raw cleanup again; a reproduced
   `cleanup,evidence,body_error,cleanup,teardown_error` sequence can repeat
   retirement or TERM/KILL;
2. setup failures after finalizer registration but before launch can produce a
   body failure plus a misleading `publication_unproven` teardown failure;
3. a timeout in the authenticated post-FreeCAD `ps` inspection overwrites the
   already captured FreeCAD buffers and is misclassified as a FreeCAD timeout;
4. the non-real endpoint regressions do not use the same production-shaped
   `VIBECAD_HOME -> data_root -> daemon_run_root -> kernel.sock` composition as
   the real harness; and
5. evidence construction or emission failure is not itself lifecycle-managed.

The independent mechanical route first attempted the authorized
terra-medium child, but both root and delegated creation were rejected with
`agent thread limit reached`. Per the recovery rules, an unrelated existing
subagent executed the exact read-only packet as a recorded route fallback.
Harness `38 passed, 1 deselected`, combined `117 passed, 2 deselected`, and
Ruff check passed. Ruff format check then stopped on the frozen probe. The
read-only formatter diff contains only four line-wrapping collapses and no
semantic change.

The following recovery anchor is now authoritative:

```text
Snapshot ID: MRG1-S19
branch: codex/agent-stage3
HEAD/upstream:
  2db503ab42e25a7f68d41c45b7151999fe53a027
index: empty
artifact-before-this-section:
  de633310d4d2d4a431cffa2cde7f27fe1ae98637ed6783728d129e4b2093ccf8
bootstrap:
  d6129e2431b262708a7662ecb27306e40878d3c0c2ba14a4135077ffc31ae63b
P0B:
  bb52f21bee4831e61588c9b489d56cc460f88f5d247b8a3a382e30773006a230
harness:
  a0da606e87554410a2c1f2859ba2d92fd0167c430cfdf87d135252adf9874e24
probe:
  fde0c459f96fc91721c7036a036fbe09c8cf8d768171f1f82e82113da1f3f3fd
RG03 budget: unconsumed
```

### 51.1 Proposed MRG1-G1-C00B-A06

Product outcome: make the real FreeCAD acceptance lifecycle at-most-once and
truthful under cleanup, prelaunch and authenticated-inspection failures, while
bringing the frozen probe through the already declared formatter gate.

Write allowlist:

```text
tests/test_freecad_workbench_bootstrap.py
tests/fixtures/freecad_workbench/bootstrap_probe.py
```

Implementation:

- cache cleanup start, outcome and error across body and finalizer so raw
  cleanup cannot run twice, including when it raises after retirement or a
  signal attempt;
- distinguish prelaunch setup failure from an attempted publication and keep
  its finalizer cleanup non-asserting;
- isolate FreeCAD timeout handling from authenticated `ps` inspection errors,
  preserving the original child return code and buffers;
- replace hand-built endpoint tests with the production-shaped path
  composition and prove preflight occurs before any subprocess;
- lifecycle-manage evidence construction/emission failure without losing
  cleanup or creating a second teardown error;
- add genuine non-real RED regressions for every item above; and
- apply only Ruff's current semantic-neutral formatting to the probe.

Gates:

1. reused sol-high coding subagent, test-first, two-path maximum;
2. focused and full non-slow harness, combined detached-test exclusion, Ruff
   check/format, whitespace and exact hash/allowlist inspection;
3. fresh sol-max adversarial review;
4. independent mechanical subagent, preferring terra-medium and recording the
   existing-thread fallback only if the platform still rejects creation; and
5. if and only if every gate is GO, one exact no-retry RG03 real invocation.

Breakers:

- any production-source, P0B, artifact-external, course-document or
  `.workbuddy/` write;
- child-derived data entering authentication, publication, retirement or
  signal eligibility;
- any repeated raw cleanup, retirement or signal attempt;
- loss or overwrite of the original FreeCAD buffers;
- any real/slow process before both independent gates are GO; or
- any anchor/hash/index drift not explained by this exact allowlist.

Approval identifier:

```text
MRG1-G1-C00B-A06
```

| Entry ID | Decision / approval | Commit / push | Gate evidence | Residual | Snapshot | State |
|---|---|---|---|---|---|---|
| MRG1-G1-C00B-E20 | sol-max A05 NO-GO plus mechanical formatter RED; A06 proposed, not approved | `not-created` | functional gates green; blocker and three majors reproduced; frozen probe has semantic-neutral Ruff diff; no real process | cleanup/evidence/prelaunch/inspection lifecycle requires correction; terra thread creation unavailable; RG03 unconsumed | MRG1-S19 | recovery approval gate open |

## 52. A06 authorization

At `2026-07-27T07:00:46-07:00`, in direct response to §51.1, the user
supplied the exact authorization:

```text
批准 MRG1-G1-C00B-A06 接下来 你自主推进吧
```

This approves the complete §51.1 recovery packet at artifact hash
`bef1f78b17a5d5e2d2ae8804168b00d074c7908d68c8a3802af9a8725dc17e95`
and authorizes the controller to proceed autonomously within that packet. It
does not expand the two-path write allowlist, permit production changes, waive
either independent gate, or permit more than the single conditional RG03
invocation.

The current capability profile and Codex adapter selection are:

```text
approval: native-plan
delegation: spawn-send-wait
persistence: repo-artifact
process: native-session-poll
adapter: Codex
actual delegation route: existing-thread followup-send-wait
```

Permitted evidence-source categories:

- `live capability declarations`: `update_plan`, collaboration follow-up/wait,
  agent creation, command execution and session polling are declared; the
  current agent-creation operation is quota-limited;
- `observable behavior`: native-plan projection, existing-agent follow-up and
  wait, and command completion have succeeded; repeated new-agent creation
  returned `agent thread limit reached`;
- `environment identity`: Codex desktop in the declared VibeCAD workspace on
  branch `codex/agent-stage3`;
- `public configuration`: the current host declares unrestricted filesystem
  access and no interactive approval requirement; no extra authority is
  inferred.

Environment residual:

```text
host/runtime identity: Codex desktop
missing capability: creation of an additional agent thread
observation evidence: repeated root and delegated spawn attempts returned
  agent thread limit reached
selected fallback or limitations: reuse existing agents with their already
  selected sol-high or sol-max runtime; prefer terra-medium only if a fresh
  thread becomes available
impact: mechanical gates may use an independent higher-tier existing agent;
  approval, allowlist, breakers and objective commands remain unchanged
observable retest condition: a future spawn_agent call succeeds without the
  thread-limit error
```

| Entry ID | Decision / approval | Commit / push | Gate evidence | Residual | Snapshot | State |
|---|---|---|---|---|---|---|
| MRG1-G1-C00B-E21 | A06; exact user authorization above; §51.1 fully bound | `not-created` | S19 anchor and five hashes reverified; HEAD/upstream `2db503a`; index empty | new terra-medium thread unavailable; existing-agent route authorized by recovery discipline; RG03 conditional and unconsumed | MRG1-S19 | approved / sol-high implementation next |

## 53. A06 implementation candidate

At `2026-07-27T07:24:36-07:00`, the reused sol-high coding agent completed
MRG1-G1-C00B-I06 within the exact two-path A06 allowlist. The controller
independently reverified the following frozen candidate:

```text
branch: codex/agent-stage3
HEAD/upstream:
  2db503ab42e25a7f68d41c45b7151999fe53a027
index: empty
artifact-before-this-section:
  b4663002043ea952584f39b598000731de460ee2c781d569172e878ba691b79a
bootstrap:
  d6129e2431b262708a7662ecb27306e40878d3c0c2ba14a4135077ffc31ae63b
P0B:
  bb52f21bee4831e61588c9b489d56cc460f88f5d247b8a3a382e30773006a230
harness:
  a9a40ce3062b3ac97d9de9ee3bd9d5dea08f64dd9473f43b33a66664db47d16f
  2231 lines / 76136 bytes
probe:
  63a5bd891adc3cbc64837df211a8f67ace6ea9bace905c661bf6e13b9ba8c74d
  163 lines / 5670 bytes
```

The genuine non-real transition was:

- cleanup blocker RED: `1 failed`, proving a second raw cleanup after a
  TERM-like side effect and `BaseException`;
- complete focused RED: `6 failed, 3 passed, 36 deselected`, with the six
  failures matching the reviewed defects rather than missing helpers or setup;
- final focused GREEN: `10 passed, 36 deselected`;
- full harness: `45 passed, 1 deselected`;
- combined non-real: `124 passed, 2 deselected`;
- four-path Ruff check and format check: pass.

The candidate now caches cleanup start/outcome/error across body and finalizer,
separates prelaunch state from publication attempts, preserves original
FreeCAD buffers across authenticated inspection failure, binds endpoint
preflight to the production-shaped runtime composition, and transfers body
ownership safely when evidence build, stringify or emit fails. The probe
change is Ruff-only; its preimage can be reconstructed byte-for-byte and the
before/after Python AST is identical.

No slow/real test, FreeCAD, daemon, real `Popen`, signal, network, install,
stage, commit or push occurred. The exact candidate is frozen for fresh
sol-max review and an independent mechanical pre-real gate. RG03 remains
unconsumed and unreachable until both gates return GO.

| Entry ID | Decision / approval | Commit / push | Gate evidence | Residual | Snapshot | State |
|---|---|---|---|---|---|---|
| MRG1-G1-C00B-E22 | approved A06 implemented by reused sol-high coding agent | `not-created` | genuine 1-fail blocker RED; focused 6/3 RED to 10 GREEN; harness 45; combined 124; Ruff pass; controller anchor/hash check | new test-harness complexity requires fresh adversarial review; RG02 first exception and existing race residuals remain | MRG1-S19 | candidate frozen / dual pre-real gates next |

## 54. R06 compound-failure RED and A06 continuation

The independent mechanical M06 gate passed against the exact §53 candidate:

- harness: `45 passed, 1 deselected`;
- combined non-real: `124 passed, 2 deselected`;
- Ruff check/format, whitespace, compile/AST, five hashes, HEAD/upstream and
  empty index: pass.

The fresh sol-max R06 review returned NO-GO with zero blockers and one major.
The first four A05 findings are closed, but an evidence build, stringify or
emit failure is currently re-raised before the existing action, transport,
parse, return-code or cleanup primary is selected. Three non-real compound
diagnostics showed `EvidenceError` masking:

1. an existing action primary;
2. `_CleanupOutcome(clean=False, detail="signal_forbidden")`; and
3. a cached cleanup `BaseException`.

Raw cleanup still occurred exactly once, but the current precedence violates
the approved requirement to lifecycle-manage evidence failure without losing
the primary action or cleanup proof. This is inside the existing A06 decision,
write allowlist and authority; it does not reopen approval.

R06 also has an execution deviation. Its first focused command used `uv run`
instead of the supplied direct virtual-environment command, refreshed the
local editable environment, emitted a package-index retry, and created cache
files that the reviewer reported removing. Repository hashes and index did not
drift. None of the R06 command results are accepted as mechanical gate
evidence; M06 is the independent mechanical record. A pre-existing ignored
`bootstrap_probe.cpython-312.pyc` dated before this review remains outside the
candidate and will not be staged.

### MRG1-S20 recovery snapshot

#### 1. Completed milestones

- A06 I06 genuine RED/GREEN and M06 mechanical PASS are recorded above;
- R06 closed four prior findings and reproduced the remaining compound-failure
  precedence major;
- HEAD/upstream remain
  `2db503ab42e25a7f68d41c45b7151999fe53a027`, index is empty, and RG03 is
  unconsumed.

#### 2. Ordered next packets

1. I06B, reused sol-high coding agent: add three genuine compound-failure
   regressions and preserve the existing action/transport/parse/return-code/
   cleanup primary, chaining evidence failure only as secondary.
2. If focused and full non-real gates pass, freeze a new harness hash; probe
   remains `63a5bd891adc3cbc64837df211a8f67ace6ea9bace905c661bf6e13b9ba8c74d`.
3. Run fresh sol-max R06B review and independent mechanical M06B.
4. Only if both are GO and all pre-run identity/readiness/cold-state breakers
   pass, consume the one-invocation RG03 budget.
5. GREEN branches to exact allowlist staging, cached gates, commit and push;
   any RED branches to a new ledger entry with no retry.

#### 3. Approved decisions

- The exact user authorization in §52 keeps MRG1-G1-C00B-A06 active.
- I06B does not expand authority: it writes only
  `tests/test_freecad_workbench_bootstrap.py`.
- Production bootstrap, P0B, probe, course documents and `.workbuddy/` remain
  read-only; the same approval must not be requested again.

#### 4. Execution discipline

- profile: `native-plan`, existing-thread `spawn-send-wait`,
  `repo-artifact`, `native-session-poll`, Codex adapter;
- coding route: sol-high; review route: sol-max; mechanical route: independent
  existing worker while fresh terra-medium creation is quota-blocked;
- no slow/real, FreeCAD, daemon, real `Popen`, signal, network, install, stage,
  commit or push before dual pre-real GO;
- breakers: any repeated cleanup/retirement/signal, evidence masking a primary,
  child data entering trust decisions, frozen-hash/index drift, out-of-scope
  write, or any premature real process.

| Entry ID | Decision / approval | Commit / push | Gate evidence | Residual | Snapshot | State |
|---|---|---|---|---|---|---|
| MRG1-G1-C00B-E23 | A06 continues unchanged after R06 compound-failure major | `not-created` | M06 PASS; R06 0 blocker/1 major; three compound cases reproduced; no RG03 | R06 used unauthorized `uv run` and local environment refresh; its command evidence excluded; ignored pre-existing pyc remains unstaged | MRG1-S20 | I06B authorized next |

## 55. I06B compound-failure candidate

At `2026-07-27T07:45:49-07:00`, the reused sol-high coding agent completed
I06B within the unchanged A06 authority and one-file continuation allowlist.
The controller independently reverified:

```text
branch: codex/agent-stage3
HEAD/upstream:
  2db503ab42e25a7f68d41c45b7151999fe53a027
index: empty
artifact-before-this-section:
  ef16b33fd8db25f4bac4bc14f55bd2dd3d011f93e4f6d58de7f13601da623558
bootstrap:
  d6129e2431b262708a7662ecb27306e40878d3c0c2ba14a4135077ffc31ae63b
P0B:
  bb52f21bee4831e61588c9b489d56cc460f88f5d247b8a3a382e30773006a230
harness:
  72b21a7014e9479f92d75e0be4ac9db7f05a51b98ffb9e938962e1cdf40efdd4
  2362 lines / 80744 bytes
probe:
  63a5bd891adc3cbc64837df211a8f67ace6ea9bace905c661bf6e13b9ba8c74d
```

The genuine compound RED covered action, clean-false cleanup proof and cleanup
`BaseException`, each combined with evidence build, stringify and emit
failure: `9 failed, 46 deselected`. The identical selection then passed
`9 passed, 46 deselected`. Final evidence was:

- lifecycle/order focused: `17 passed, 38 deselected`;
- full harness: `54 passed, 1 deselected`;
- combined non-real: `133 passed, 2 deselected`;
- Ruff check/format and whitespace: pass.

The candidate records evidence failure instead of immediately re-raising it,
selects the existing action, timeout, parse, return-code or cleanup primary,
and raises that primary with evidence failure as its explicit cause. Evidence
failure is primary only when no earlier failure exists. Body ownership and
the shared at-most-once cleanup cache remain intact.

No slow/real test, FreeCAD, daemon, real `Popen`, signal, network, install,
`uv`, stage, commit or push occurred. The harness and probe are frozen for
fresh R06B and M06B. RG03 remains unconsumed.

| Entry ID | Decision / approval | Commit / push | Gate evidence | Residual | Snapshot | State |
|---|---|---|---|---|---|---|
| MRG1-G1-C00B-E24 | A06 I06B compound precedence correction | `not-created` | genuine 9-case RED to GREEN; focused 17; harness 54; combined 133; Ruff pass; controller hash/anchor check | R06 command evidence remains excluded; fresh R06B/M06B required | MRG1-S20 | candidate frozen / final dual pre-real gates next |

## 56. Final dual pre-real GO and standing non-product authority

At `2026-07-27T07:52:41-07:00`, final R06B and M06B completed against the
exact §55 candidate and artifact hash
`6c44de51f68d028eba114d0dc61ea45e933659bd7c3492777520d1111b05f4fa`.

R06B returned GO with zero blocker, major or minor findings. Its closure
matrix confirmed:

- shared cleanup state prevents raw cleanup/finalizer re-entry;
- prelaunch primary errors cannot acquire a misleading publication teardown;
- authenticated inspection failures preserve the FreeCAD result;
- endpoint preflight uses the production-shaped runtime composition;
- evidence-only failure remains the exact primary object; and
- compound evidence failure is an explicit cause of the earlier
  action/transport/parse/return-code/cleanup primary.

Fresh valid R06B evidence was `9 passed, 46 deselected`, a six-closure
selection of `19 passed, 36 deselected`, and an eight-family in-memory
precedence/identity/traceback/finalizer diagnostic. An initial version of the
in-memory diagnostic stopped locally with a Python `NameError` before any
candidate assertion; the corrected diagnostic passed and caused no state
change. No `uv`, network, install, bytecode or repository write occurred.

M06B independently passed:

- harness `54 passed, 1 deselected`;
- combined non-real `133 passed, 2 deselected`;
- Ruff check/format, whitespace and in-memory compile/AST: pass;
- candidate hashes, HEAD/upstream and empty index: exact.

The user then supplied this standing execution direction:

```text
接下来的工作你尽力推进  不涉及产品功能和形态变化 无需我审批
```

This direction permits autonomous test hardening, diagnostics, formatting,
CI/gate work and ledger corrections that do not change product functionality,
user-facing shape or an external contract, provided each action remains inside
its recorded allowlist, gates and breakers. Product behavior, interaction
shape, public contract changes, destructive actions and expanded real-run
budgets still reopen approval. This standing direction does not weaken the
one-shot RG03 rule.

All §48.3/§51.1 branch conditions are now satisfied. RG03 is reachable for
exactly one invocation:

```text
VIBECAD_RUN_INTEGRATION=1
VIBECAD_FREECAD_ENV=/Users/wangtao/Library/Application Support/VibeCAD/runtime/mamba/envs/vibecad
PYTHONPATH=src
PYTHONDONTWRITEBYTECODE=1
.venv/bin/python -B -m pytest -q -s -p no:cacheprovider -m slow \
  tests/test_freecad_workbench_bootstrap.py::test_real_freecad_embedded_interpreter_bootstraps_and_retires_one_daemon
```

Before pytest starts, the sol-max runner must match the canonical managed
prefix, receipt/readiness, two generation captures, regular executable
FreeCAD host, repository hashes, empty index and cold process/residue state.
A preflight breaker does not consume RG03. Once pytest starts, any result
consumes RG03 and forbids retry. Only the harness's authenticated expected-ID
cleanup and bounded fallback may signal; the runner must never signal
manually.

| Entry ID | Decision / approval | Commit / push | Gate evidence | Residual | Snapshot | State |
|---|---|---|---|---|---|---|
| MRG1-G1-C00B-E25 | A06 plus standing non-product execution direction; RG03 branch reached | `not-created` | R06B GO 0/0/0; M06B harness 54, combined 133, all mechanical checks PASS; hashes/index exact | existing generic final-read-to-launch and Darwin check-to-signal intervals; RG02 first exception unproven; one RG03 invocation remains | MRG1-S20 | final preflight / RG03 next |

## 57. Non-consuming RG03 preflight correction

At `2026-07-27T08:00:23-07:00`, the first RG03 Phase-A packet matched the
repository anchor, five hashes, canonical managed prefix and exact managed
receipt, then stopped before later checks because it required
`paths.bound_external_prefix()` to equal the managed prefix. The observed
value was `None`. Pytest invocation count remained zero; no FreeCAD, daemon,
`Popen`, signal or state change occurred, so RG03 remains unconsumed.

Read-only sol-max D03 proved that the packet condition conflated two exclusive
runtime-selection branches:

- an existing standard managed `paths.env_prefix()` is selected before any
  external binding and uses its own `.vibecad_ready`; and
- an external runtime requires the separate identity-pinned external receipt.

The current canonical prefix is the standard managed `env_prefix`.
`user_override_env()` is `None`, `active_runtime_prefix()` equals that
prefix, the managed receipt is exact, receipt state is `CURRENT`, recovery is
`READY`, two generation captures match, and Python plus `freecadcmd` targets
are regular and inside the prefix. The default external receipt is an
unrelated stale legacy binding whose recorded device no longer matches; it is
correctly rejected and has no role in the managed branch. It remains
untouched as an environment residual.

The corrected Phase-A managed invariant is:

```text
no override
canonical == env_prefix == active_runtime_prefix
ready_sentinel == canonical/.vibecad_ready
managed receipt == expected managed receipt
receipt state CURRENT and runtime_ready true
two generation captures equal and bind canonical managed Python
managed Python target and freecadcmd are regular, executable as applicable,
and resolve inside canonical
```

The real test subsequently switches to isolated `VIBECAD_HOME`, sets the
explicit prefix override, captures fresh evidence, writes and verifies a new
isolated external receipt, and only then invokes FreeCAD. Therefore default
home external binding is neither necessary nor sufficient for this gate.

This is a non-product gate-packet correction under the standing §56 direction;
it changes no code, product behavior, candidate hash or real-run budget. The
runner must repeat the complete non-consuming Phase A with this corrected
branch, including the previously unreached cold process/residue checks. Only
then may it start the unchanged one-shot RG03 command.

| Entry ID | Decision / approval | Commit / push | Gate evidence | Residual | Snapshot | State |
|---|---|---|---|---|---|---|
| MRG1-G1-C00B-E26 | §56 standing non-product authority; Phase-A condition corrected | `not-created` | first Phase A stopped at invalid external-binding requirement; D03 GO; pytest count 0; hashes/index exact | stale rejected legacy external receipt remains untouched; no effect while managed env_prefix is selected | MRG1-S20 | RG03 unconsumed / corrected full Phase A next |

## 58. RG03 real environment GREEN

At `2026-07-27T08:04:22-07:00`, the corrected full Phase A passed and RG03
started exactly once. The original controllable pytest session was retained
until completion and was never relaunched.

Exact command:

```text
VIBECAD_RUN_INTEGRATION=1
VIBECAD_FREECAD_ENV=/Users/wangtao/Library/Application Support/VibeCAD/runtime/mamba/envs/vibecad
PYTHONPATH=src
PYTHONDONTWRITEBYTECODE=1
.venv/bin/python -B -m pytest -q -s -p no:cacheprovider -m slow \
  tests/test_freecad_workbench_bootstrap.py::test_real_freecad_embedded_interpreter_bootstraps_and_retires_one_daemon
```

Result:

```text
invocations: 1 / 1
exit: 0
pytest: 1 passed in 2.36s
child return: 0
probe status: ok
timed out: false
endpoint bytes: 66
daemon process: <canonical managed python> -B -m vibecad.daemon
cleanup: clean=true, detail=retired, retire_attempted=true
TERM sent: false
KILL sent: false
```

The bounded probe record proved the repository bootstrap and probe sources,
empty preloaded VibeCAD modules, exact repository source insertion, managed
FreeCAD `sys.executable`/prefix, cold absent run root, successful client close,
published daemon ID/PID and the expected isolated run-root/socket. Parent
evidence proved all runtime checks true, an authenticated daemon birth token,
no action/inspection/parse/cleanup error, and exact authenticated retirement.

Post-run Phase C found no FreeCAD, `freecadcmd` or `vibecad.daemon` process,
no `/private/tmp/vc-c00b-*` residue, no socket/receipt/run-root residue, and no
repository hash, HEAD/upstream, index or status drift. There was no retry,
manual signal, `uv`, network, install, bytecode, repository write, stage,
commit or push. RG03 is consumed successfully.

### MRG1-S21 recovery snapshot

#### 1. Completed milestones

- C00B production selector and safety harness are complete at the five hashes
  below;
- R06B returned GO with zero findings, M06B passed all mechanical gates, and
  RG03 passed one real managed FreeCAD invocation with authenticated
  retirement and zero residue;
- HEAD/upstream remain
  `2db503ab42e25a7f68d41c45b7151999fe53a027`; index is empty before staging.

#### 2. Ordered next packets

1. Stage exactly artifact, bootstrap, P0B, harness and probe.
2. Run the cached staged C00B non-real/Ruff/whitespace/hash gate.
3. On PASS, commit once as
   `fix(daemon): bind embedded bootstrap to managed Python` and push.
4. Verify HEAD/upstream equality and that the Release workflow does not run
   for this branch push.
5. Bind commit/push evidence in the next G1-C00 preamble before any next
   semantic staging.

#### 3. Approved decisions

- MRG1-G1-C00B-A06 and the standing §56 non-product execution direction
  remain active for this exact closeout.
- No source path beyond the five-path C00B stage allowlist is authorized.
- The real-run budget is exhausted successfully; no further C00B real
  invocation is permitted or needed for this commit.

#### 4. Execution discipline

- profile: `native-plan`, existing-thread `spawn-send-wait`,
  `repo-artifact`, `native-session-poll`, Codex adapter;
- exact staging only; never use broad add; excluded `.workbuddy/` and course
  documents remain untouched and unstaged;
- breakers: staged-path mismatch, cached gate red, source/test hash drift,
  process residue, commit mismatch or push failure;
- stale rejected default external receipt remains an untouched environment
  residual and cannot enter this commit.

Frozen pre-ledger hashes:

```text
artifact:
  86028a5e2afff9feee566a14e19160e84e8bfc4dff90ab31e667172328870e8b
bootstrap:
  d6129e2431b262708a7662ecb27306e40878d3c0c2ba14a4135077ffc31ae63b
P0B:
  bb52f21bee4831e61588c9b489d56cc460f88f5d247b8a3a382e30773006a230
harness:
  72b21a7014e9479f92d75e0be4ac9db7f05a51b98ffb9e938962e1cdf40efdd4
probe:
  63a5bd891adc3cbc64837df211a8f67ace6ea9bace905c661bf6e13b9ba8c74d
```

| Entry ID | Decision / approval | Commit / push | Gate evidence | Residual | Snapshot | State |
|---|---|---|---|---|---|---|
| MRG1-G1-C00B-E27 | A06 RG03 consumed successfully; exact C00B closeout authorized | pending exact commit/push | R06B GO; M06B PASS; RG03 exit 0 / 1 passed / authenticated retired / zero signal and residue | generic final-read-to-launch and Darwin check-to-signal intervals; RG02 first exception unproven; stale rejected legacy receipt untouched | MRG1-S21 | real GREEN / exact stage and cached gate next |

## 59. Staged-gate process-filter correction

At `2026-07-27T08:09:06-07:00`, SG01 proved the exact five-path stage, no
unstaged delta on those paths, staged-blob/worktree/frozen hash equality,
cached harness `54 passed, 1 deselected`, combined `133 passed, 2 deselected`,
Ruff check/format, whitespace and in-memory compile/AST. It then stopped
because its `ps | awk` residue filter matched the filter's own `zsh` and
`awk` command lines.

The two reported PIDs were inspection processes, not FreeCAD or
`vibecad.daemon`. No candidate, index or product process changed, and RG03 was
not rerun. This is a mechanical gate-packet self-match under the standing §56
non-product authority.

SG01B must first reverify the exact staged set and all five staged/worktree
hashes. It may then replace only the ambiguous residue check with:

```text
pgrep -fal '[F]reeCAD|[f]reecadcmd|[v]ibecad[.]daemon'
find /private/tmp -maxdepth 1 -type d -name 'vc-c00b-*' -print
```

The bracketed process pattern cannot match its own literal command line.
Expected results are `pgrep` exit 1 with no matches and `find` exit 0 with no
output. If the stage and source/test hashes remain exact, the already completed
SG01 test/static evidence remains bound to the same bytes and does not need
another execution. Any actual process/residue match, staged mismatch or
unstaged candidate delta stops closeout.

| Entry ID | Decision / approval | Commit / push | Gate evidence | Residual | Snapshot | State |
|---|---|---|---|---|---|---|
| MRG1-G1-C00B-E28 | §56 non-product gate correction; SG01 self-match | pending exact commit/push | staged blobs/hash PASS; harness 54; combined 133; Ruff/format/AST PASS; residue check ambiguous from two self-matches | no actual product residue proven; corrected self-excluding SG01B required | MRG1-S21 | artifact restage / SG01B next |

## 60. SG01B staged closeout PASS

SG01B reverified the exact five-path stage, no unstaged candidate delta and
staged-blob/worktree/frozen hash equality. `git diff --cached --check` passed.
The corrected residue commands returned:

```text
pgrep bracketed product/daemon pattern: exit 1, no output
find /private/tmp/vc-c00b-*: exit 0, no output
```

SG01's cached `54 passed, 1 deselected`, `133 passed, 2 deselected`, Ruff
check/format and compile/AST evidence remains bound to identical source/test
bytes. RG03 was not rerun. The candidate is ready for the single prewritten
commit:

```text
fix(daemon): bind embedded bootstrap to managed Python
```

The publish helper's full draft-PR workflow is not applicable to this
approved closeout: local `gh` is not authenticated and no PR was authorized.
The controller may use the repository's existing Git remote authentication
for the approved commit and push only. No PR will be created.

The final artifact hash and commit/push fact cannot be included in their own
commit. Per §28.6 and S21, the next G1-C00 preamble must bind those values
before any subsequent semantic staging.

| Entry ID | Decision / approval | Commit / push | Gate evidence | Residual | Snapshot | State |
|---|---|---|---|---|---|---|
| MRG1-G1-C00B-E29 | A06/S21 exact closeout | pending exact commit/push | SG01B exact stage/hash/whitespace/zero-residue PASS; cached 54/133/Ruff/AST; RG03 GREEN | final commit/push fact deferred to next preamble; no PR authorized | MRG1-S21 | commit and push next |

## 61. C00B closeout fact and G1-C00 execution preamble

At `2026-07-27T08:14:41-07:00`, C00B closed:

```text
commit:
  27e95461185dde7da8d72d21d58ea78e576ba288
subject:
  fix(daemon): bind embedded bootstrap to managed Python
commit artifact:
  1a410ec645b71237cc2c3dec67e2a3131c522d423c5da149c7dac55b252e52d2
push:
  origin/codex/agent-stage3
HEAD/upstream:
  equal at 27e95461185dde7da8d72d21d58ea78e576ba288
Release workflow push runs for this SHA:
  0
```

The remote's moved-location response was normalized locally to
`https://github.com/wangtao9090/VibeCAD.git` and the exact upstream branch was
read back at the pushed commit. No PR was created. Only the three excluded
untracked paths remain outside Git.

MRG1-A04 explicitly approved G1 `C00` through `C04`; therefore G1-C00 may
start without another approval. The user's §56 standing direction additionally
permits its non-product test, diagnostic, formatting, gate and ledger work,
but the product implementation remains bounded by the already approved A04
contract below.

### 61.1 Context and success criteria

G1-C00 registers the repository's classic `Mod/VibeCAD` thin-client addon
without connecting to the daemon or importing GUI/runtime ownership into the
presenter. Success means:

- `Init.py` is a headless no-op;
- `InitGui.py` registers exactly one Workbench and performs no daemon
  connection at import time;
- package metadata describes one repository-local VibeCAD addon; and
- presenter state is a Qt-, FreeCAD- and durable-store-independent projection
  of public mappings.

The public 28-tool/six-operation surface and all daemon/application/store
contracts remain unchanged.

### 61.2 Decisions and commit sequence

Active decisions are MRG1-D17, D19, D21 and D22 under exact authorization
MRG1-A04. One behavior commit is allowed:

| ID | Commit | Scope | Independent gate |
|---|---|---|---|
| G1-C00 | `feat(workbench): register thin-client FreeCAD addon` | classic addon registration, package metadata, pure presenter projection and fake-host/package/controller tests | genuine absent-addon RED; G1-G00 pytest/Ruff; fresh sol-max review; independent mechanical staged gate |

No packaging, Addon Manager publication, installation into the user's normal
FreeCAD tree, daemon connection, Qt worker or real GUI launch belongs to C00.

### 61.3 Manual validation and expected impact

C00 has no user-present or real-GUI action. Real addon discovery,
registration, activation, thread identities, daemon retirement and zero
residue are owned by G1-M00 before C01 grows beyond its minimal Dock.

Expected automated impact is two new focused test modules and one fake host.
No existing test is deleted, merged or weakened. The RED must arise because
the addon and presenter contracts are absent, not because of import setup or a
missing test dependency.

### 61.4 Budget, allowlist and breakers

Commit budget: one. Exact controller-owned allowlist:

```text
M docs/orchestrated/vibecad-multi-runtime-g1.md
A freecad/VibeCAD/Init.py
A freecad/VibeCAD/InitGui.py
A freecad/VibeCAD/package.xml
A freecad/VibeCAD/vibecad_workbench/__init__.py
A freecad/VibeCAD/vibecad_workbench/state.py
A tests/fixtures/freecad_workbench/fake_host.py
A tests/test_freecad_workbench_package.py
A tests/test_freecad_workbench_controller.py
```

Breakers:

- any import-time daemon connection, runtime installation or process launch;
- Qt, FreeCAD or durable-store imports in `state.py`;
- more than one registration under repeated `InitGui.py` execution;
- an out-of-allowlist write, existing-contract change or public-surface drift;
- an unexpected RED, failed focused/static gate, review blocker/major or
  staged-path mismatch; or
- any real FreeCAD/GUI action before the separately bounded M00 packet.

Exact G1-G00 command:

```text
.venv/bin/python -m pytest -q \
  tests/test_freecad_workbench_package.py \
  tests/test_freecad_workbench_controller.py
.venv/bin/python -m ruff check \
  freecad/VibeCAD/Init.py \
  freecad/VibeCAD/InitGui.py \
  freecad/VibeCAD/vibecad_workbench \
  tests/fixtures/freecad_workbench/fake_host.py \
  tests/test_freecad_workbench_package.py \
  tests/test_freecad_workbench_controller.py
```

### 61.5 Residuals and execution route

Real FreeCAD discovery/activation, Dock responsiveness, GUI worker isolation,
clean GUI shutdown and installer/packaging behavior remain deferred to their
named M00/C01/follow-on gates. This commit cannot claim a complete vertical
slice or durable beta.

Implementation uses the existing sol-high coding subagent, fresh sol-max
adversarial review and an independent mechanical worker. Fresh terra-medium
thread creation remains quota-blocked, so the recorded existing-thread
fallback is permitted without weakening the gate. The controller alone edits
this artifact, stages, commits and pushes.

| Entry ID | Decision / approval | Commit / push | Gate evidence | Residual | Snapshot | State |
|---|---|---|---|---|---|---|
| MRG1-G1-C00-E01 | MRG1-A04 exact C00 scope; C00B closeout bound above | `not-created` | HEAD/upstream `27e9546`; index empty; absent addon confirmed; exact allowlist/gates frozen | real discovery/GUI/M00, packaging and full vertical slice remain open | MRG1-S21 | approved execution / sol-high RED-first packet next |

## 62. G1-C00 D00 exact thin-addon contract

At `2026-07-27T08:25:16-07:00`, a read-only sol-max D00 review returned GO
with no blocker or major finding and no need for an existing-API or allowlist
change.

The five product files are frozen as follows:

- `Init.py` and `vibecad_workbench/__init__.py` contain only module
  docstrings;
- `InitGui.py` imports only `FreeCADGui`, defines `VibeCADWorkbench` with
  `MenuText = "VibeCAD"`, `ToolTip = "VibeCAD thin client"`, no-op
  `Initialize()` and `GetClassName() == "Gui::PythonWorkbench"`;
- registration uses host-persistent slot
  `_vibecad_workbench_instance`, calls `FreeCADGui.addWorkbench()` only when
  the slot is absent, and writes the slot only after successful registration;
- `package.xml` uses format 1, project version `0.6.0`, MIT, Wang Tao and the
  canonical repository URL, and declares exactly one
  `VibeCADWorkbench` workbench without an icon or dynamic date; and
- `state.py` imports only stdlib `dataclasses` and `re`.

The exact `state.py` public surface is:

```text
ProjectionError
ProjectSummary(project_id, generation, revision_id, manifest_sha256)
ProjectPage(projects, next_cursor)
TaskSummary(
  task_id, project_id, generation, base_revision, reasoning_owner,
  review_policy, status, next_action, candidate_revision,
  committed_revision, draft_id
)
TaskPage(tasks, next_cursor)
project_page_from_mapping(value)
task_page_from_mapping(value)
```

All four summaries/pages are frozen slotted dataclasses; arrays become tuples
and no input container reference is retained. Project parsing accepts only
the exact public success envelope and nested schema-v1 project/result shapes.
Task parsing accepts only the exact public success envelope and task/result
shapes. Every container must be exact `dict`/`list`; unknown or missing keys,
subclasses, booleans-as-integers, generation outside
`0..9007199254740991`, invalid fixed-format IDs/digests, empty non-null
cursors, unsorted/duplicate primary IDs or invalid optional revisions/drafts
fail closed as:

```text
ProjectionError("invalid public mapping")
```

The parser never echoes input. Task enum-like presentation strings remain
non-empty opaque public strings; C00 does not copy private server enums.

The fake host is limited to `FakeWorkbench`, a `ModuleType("FreeCADGui")`
with an `added_workbenches` list and append-only `addWorkbench`, plus a fixture
that installs it in `sys.modules`. Tests execute `InitGui.py` through
`runpy.run_path(..., init_globals={"Workbench": FakeWorkbench})`; product
imports are late-loaded inside tests so the absent-addon RED collects cleanly.

Exact test inventory:

```text
tests/test_freecad_workbench_package.py:
  test_classic_addon_layout_is_complete
  test_package_xml_declares_local_vibecad_workbench
  test_init_and_workbench_package_imports_are_side_effect_free

tests/test_freecad_workbench_controller.py:
  test_init_gui_registers_exactly_one_workbench_across_reexecution
  test_init_gui_import_boundary_excludes_daemon_qt_and_store
  test_state_module_import_boundary_excludes_freecad_qt_daemon_and_store
  test_project_page_from_mapping_projects_and_detaches_public_response
  test_task_page_from_mapping_projects_and_detaches_public_response
  test_projection_rejects_malformed_public_mappings (8 parameters)
  test_projection_error_is_stable_and_does_not_echo_input
```

This is ten test functions and seventeen collected cases. The genuine RED
runs the registration and project-projection sentinels and expects two
failures because `InitGui.py` and `state.py` are absent, not because of
collection, dependency or setup failure. GREEN requires the same two to pass,
then all seventeen cases and the exact G1-G00 Ruff gate.

C01 may consume these project/task page projections without modifying
`state.py`. Selection, connection, preview, review and mutable UI state remain
excluded for C01 through C04. Real host injection, addon discovery and
activation remain M00 residuals.

| Entry ID | Decision / approval | Commit / push | Gate evidence | Residual | Snapshot | State |
|---|---|---|---|---|---|---|
| MRG1-G1-C00-E02 | A04 C00; sol-max D00 exact contract | `not-created` | D00 GO; exact 8 non-artifact paths; 10 functions/17 cases; genuine two-sentinel RED frozen | fake host only until M00; package metadata not Addon Manager proof; host marker collision low risk | MRG1-S21 | design frozen / sol-high implementation next |

## 63. G1-C00 I01 candidate

At `2026-07-27T08:36:24-07:00`, the reused sol-high coding agent completed
I01 within the exact eight-path non-artifact allowlist. The controller
reverified HEAD/upstream
`27e95461185dde7da8d72d21d58ea78e576ba288`, empty index, the single
controller-owned artifact modification and exactly eight new candidate paths.

The genuine RED and GREEN were:

```text
sentinel RED:
  2 failed after normal collection
  exact causes: absent InitGui.py and absent state.py
identical sentinel GREEN:
  2 passed
full focused:
  17 passed
Ruff check:
  PASS
Ruff format check:
  7 files already formatted
compile/AST, whitespace and allowlist:
  PASS
```

Frozen candidate hashes:

```text
Init.py:
  5fdc5f3e83d877e1195c9917e2ed7266838990afc6cc0a82eb2877834c6a0f68
InitGui.py:
  bb54c3c451a8877852e7a99f4933bea5713ce3c467e80f9571ff39f0042992a1
package.xml:
  55f0358375354018e4ad572317f93a5a3d317a58f2547c82c0611af1c1919c4f
vibecad_workbench/__init__.py:
  7053954476c0a23fb3eba1fdaf706ba9bef43fec8df1aac3b5af74894900fb9a
state.py:
  2af5c3002cb5daa4bb33ebec33c5783a4e45087109f2cb8aea23d7c85fcf90cc
fake_host.py:
  5a4f9217a38a2c1706ce912670a44bfa84fe622123b99a6873720cc4773073d9
package tests:
  33a942757443c8e8d13064555758ce070681ab6e31866355c240746736fdb1ba
controller tests:
  fd21e30b4de80b86cd833746bd801f99ab5809c26b89a1fc775ff926b57c8d3e
```

`InitGui.py` imports only `FreeCADGui` and performs host-persistent idempotent
registration. `state.py` imports only `re` and `dataclasses`, creates frozen
tuple-backed projections, validates exact public mapping shapes and emits only
the stable non-echo `ProjectionError`. No existing product/public API changed.

No real FreeCAD/GUI/daemon, process, signal, network, install, stage, commit or
push occurred. The exact candidate is frozen for fresh sol-max review and an
independent mechanical gate.

| Entry ID | Decision / approval | Commit / push | Gate evidence | Residual | Snapshot | State |
|---|---|---|---|---|---|---|
| MRG1-G1-C00-E03 | A04 C00; D00 design implemented by sol-high | `not-created` | genuine 2-fail RED to 2-pass GREEN; full 17; Ruff/format/AST/whitespace PASS; controller hash/allowlist check | fake host only until M00; independent review and mechanical gate required | MRG1-S21 | candidate frozen / dual gate next |

## 64. G1-C00 R01 recovery

The fresh sol-max R01 review returned NO-GO before staging or commit. It found
one blocker, two majors and two minors:

- the package layout test required exact equality with C00's five files and
  would therefore fail deterministically when the already approved C01 adds
  `gateway.py`, `dock.py` and `host.py`;
- `InitGui.py` inherited a fake-only `FreeCADGui.Workbench` attribute rather
  than the classic host-injected `Workbench` global frozen by D00;
- the host-persistent marker was written before `addWorkbench()` succeeded,
  so one registration exception permanently suppressed retry in that host
  session;
- `ToolTip` did not equal the frozen `"VibeCAD thin client"` value and was not
  asserted; and
- an exact `dict` containing a `str`-subclass key equal to a legal field name
  passed projection validation.

All other reviewed boundaries passed: docstring-only headless modules, the
single `FreeCADGui` import, package metadata, public response field topology,
generation/ID/digest validation, ordering and duplicate rejection, frozen
tuple-backed detachment, stable non-echo errors and the C01 projection fields.
Cursor grammar, closed presentation enums and page-size limits remain owned by
the upstream public facade; C00 continues to treat the already validated
strings as opaque.

I02 is authorized by MRG1-A04 and the user's standing non-product direction.
It may modify only:

```text
freecad/VibeCAD/InitGui.py
freecad/VibeCAD/vibecad_workbench/state.py
tests/fixtures/freecad_workbench/fake_host.py
tests/test_freecad_workbench_package.py
tests/test_freecad_workbench_controller.py
```

I02 must first add regression tests that fail for the reviewed causes, then:

- make the layout assertion a required-file subset check so future approved
  addon modules do not invalidate C00;
- execute `InitGui.py` with an injected `Workbench`, use
  `getattr(FreeCADGui, slot, None) is None`, call `addWorkbench()` on a local
  instance and write the marker only after success;
- prove one failed registration leaves no marker and a later execution retries
  successfully, while successful reexecution still registers once;
- restore and assert the exact tooltip;
- reject non-exact-string mapping keys at every validated dictionary level;
  and
- retain every previously passing projection, metadata and import boundary.

After I02, all focused tests, exact Ruff/format checks, compile/XML/whitespace
checks, a fresh sol-max review and an independent mechanical gate must pass on
new frozen hashes. R01 consumed no real FreeCAD/GUI/daemon action and caused
no repository mutation.

| Entry ID | Decision / approval | Commit / push | Gate evidence | Residual | Snapshot | State |
|---|---|---|---|---|---|---|
| MRG1-G1-C00-E04 | A04 C00; R01 sol-max recovery within exact five-path subset | `not-created` | R01 NO-GO: 1 blocker / 2 major / 2 minor; anchor and original hashes stable; no stage | real host remains M00; upstream facade owns cursor/enums/page limit | MRG1-S21 | correction required / sol-high I02 next |

## 65. G1-C00 I02 corrected candidate

The reused sol-high coding agent completed I02 without expanding the
five-path recovery subset. The regression sentinel first returned
`4 failed, 1 passed`: the failures were the reviewed classic-host protocol,
registration retry and exact-key defects; the package subset assertion was
already a passing test-only correction. The identical sentinel then returned
`5 passed`.

The resulting candidate:

- inherits the classic host-injected `Workbench` and retains only the
  `FreeCADGui` import;
- uses the exact tooltip and writes the persistent marker only after
  `addWorkbench()` succeeds;
- retries after a synthetic first registration failure and remains
  idempotent after success;
- accepts future approved addon files while still requiring every C00 file;
- removes the fake-only `FreeCADGui.Workbench` attribute and executes
  `InitGui.py` through `runpy.run_path()` with an injected
  `FakeWorkbench`; and
- rejects non-exact-string keys at every validated public mapping layer.

Corrected gate evidence:

```text
focused pytest:
  19 passed
Ruff check:
  PASS
Ruff format check:
  7 files already formatted
compile/AST/XML:
  PASS (7 Python paths; package VibeCAD)
git diff --check and eight no-index whitespace checks:
  PASS
```

Frozen candidate hashes:

```text
Init.py:
  5fdc5f3e83d877e1195c9917e2ed7266838990afc6cc0a82eb2877834c6a0f68
InitGui.py:
  582e255816a9f0f966fd9e62956d111ac445a66dcd4a25be8b4be2226e0b365c
package.xml:
  55f0358375354018e4ad572317f93a5a3d317a58f2547c82c0611af1c1919c4f
vibecad_workbench/__init__.py:
  7053954476c0a23fb3eba1fdaf706ba9bef43fec8df1aac3b5af74894900fb9a
state.py:
  0ec5454d51823d897e857ab053deb2c90c1bc32d5c3e977eb3c8617380378113
fake_host.py:
  b44398364fab68e04a1b0c7d1e6682381dd5a1f0090cb2d24ad3316d34370e12
package tests:
  1e35797129fcd3b05bc8ffa86fdfc8182f83eabb57efab55c84d5535b39eadd6
controller tests:
  ae0ad01a9e89f5affc6825585e24f018eee53260f54bd11c5d0b181906ad0aa1
```

The controller independently read back the corrected sources, hashes, empty
index and unchanged HEAD/upstream
`27e95461185dde7da8d72d21d58ea78e576ba288`. No real FreeCAD/GUI/daemon,
network, install, process, signal, stage, commit or push occurred. Fresh
sol-max semantic review and a new independent mechanical gate are required
because five hashes changed.

| Entry ID | Decision / approval | Commit / push | Gate evidence | Residual | Snapshot | State |
|---|---|---|---|---|---|---|
| MRG1-G1-C00-E05 | A04 C00; R01 exact recovery implemented by sol-high | `not-created` | genuine 4-fail/1-pass sentinel to 5-pass; full 19; Ruff/format/AST/XML/whitespace PASS; controller readback | real classic host remains M00; fresh dual gate required | MRG1-S21 | corrected candidate frozen / R02+M02 next |

## 66. G1-C00 accepted pre-staging gates

Fresh sol-max R02 returned GO with zero blocker, major, minor or nit. It
independently reproduced the registration matrix for absent, null, existing,
failed and successful host slots; verified the exact import boundary and
public-facade response topology; and closed every R01 finding. Its focused
run returned `19 passed`, Ruff passed and all nine frozen hashes matched.

R02 retained two explicitly disclosed non-findings:

- cursor grammar, closed presentation enums and the page-size bound are
  authenticated public-facade responsibilities rather than claims of the C00
  projection; and
- a synthetic host that registers an instance and then throws is not assumed
  atomic. Real discovery and registration behavior remains a named M00 gate.

Independent M02 also passed:

```text
branch:
  codex/agent-stage3
HEAD/upstream:
  27e95461185dde7da8d72d21d58ea78e576ba288
index:
  empty
focused pytest:
  19 passed
Ruff / format:
  PASS / 7 files already formatted
compile/AST/XML:
  PASS
git diff --check / eight no-index whitespace checks:
  PASS
pre/post anchor, status and nine hashes:
  identical
```

Fresh terra-medium thread creation remained quota-blocked, so M02 used the
recorded independent existing-thread fallback. Neither gate changed state or
ran real FreeCAD/GUI/daemon behavior. The controller may now append this
evidence, stage exactly the Section 61 nine-path allowlist and require one
post-stage mechanical readback before committing
`feat(workbench): register thin-client FreeCAD addon`.

| Entry ID | Decision / approval | Commit / push | Gate evidence | Residual | Snapshot | State |
|---|---|---|---|---|---|---|
| MRG1-G1-C00-E06 | A04 C00; corrected R02+M02 acceptance | `not-created`; exact stage next | R02 GO 0/0/0/0; M02 PASS; exact 19; Ruff/format/AST/XML/whitespace and frozen hashes PASS | real host atomicity/discovery remains M00; facade-owned cursor/enums/page bound | MRG1-S21 | accepted candidate / staged gate next |

## 67. G1-C01 D00 public-kernel and M00 preflight

In parallel with the read-only C00 gates, a sol-max architecture preflight
returned GO with zero blocker or major. It found that the existing public
client/facade surface is sufficient for C01 without changing a shared seam:

```text
connect/identity:
  LocalAgentClient.open(), daemon_id, ping()
discovery:
  list_projects_request(), list_tasks_request()
refresh:
  get_project_request(), get_task_request()
review transport:
  accept_draft_request(), reject_draft_request()
shutdown:
  close()
```

Project and Task discovery use pages of 50 and cursor-driven incremental
loading. `list_tasks` has no server-side project/status filter, so the worker
must paginate incrementally and filter locally; it must never synchronously
scan the complete store on the GUI thread. C01 may provide accept/reject
transport for later consumption but exposes no review action. C03 owns
fresh-authority policy. A review transport exception has unknown outcome:
close/reconnect/read durable state later and never replay automatically.

### 67.1 Thread and mapping boundary

The fixed ownership topology is:

```text
Qt main thread:
  ReviewDock, FreeCADGui, widgets and selection
    -> queued exact plain command mapping
one QThread:
  _GatewayWorker -> KernelGateway -> one LocalAgentClient
    -> queued exact plain event mapping
Qt main thread:
  validate latest request/selection and update widgets
```

The client is created, called and closed only in the worker. The worker holds
no widget, `FreeCADGui`, Document or Selection. The main thread performs no
socket call, client close, daemon retirement or blocking `QThread.wait()`.
Deactivate queues close; `closed` causes `thread.quit()` and asynchronous
destruction. Late events whose request or selection identity is no longer
current are discarded.

Commands are exact schema-v1 dictionaries for `connect`, `list_projects`,
`list_tasks`, `refresh_project`, `refresh_task`, `review` and `close`.
Events are exact schema-v1 dictionaries for `connected`, `projects`, `tasks`,
`project`, `task`, `review`, `closed` or a stable non-echo `error`. Only exact
plain dict/list/scalar values cross signals; no client, exception, QObject,
Path, dataclass or FreeCAD object may cross. Gateway validation copies the
mapping under a depth/node budget.

### 67.2 Exact C01 path responsibilities

- `InitGui.py` retains C00 registration. `Initialize()` stays disconnected;
  `Activated()` and `Deactivated()` late-import the host activation functions.
- `gateway.py` exports only `KernelGateway`. Its first worker-thread connect
  creates one client; every subsequent operation and close must use that
  thread. It accepts a closed command set and never retries review.
- `dock.py` exports only `ReviewDock`, with stable object name
  `VibeCADReviewDock`, connection state, project selector,
  awaiting-review-task selector and Refresh. There is no Accept/Reject action.
- `host.py` exports only `activate_workbench`, `deactivate_workbench` and
  `workbench_snapshot`; it owns one Dock, one worker and one QThread, uses the
  FreeCAD-provided PySide compatibility namespace, and returns only plain
  lifecycle diagnostics.
- the fake host adds deterministic main-window/dock and queued-event
  facilities while failing immediately on worker-widget or main-thread-client
  access. It cannot claim real Qt behavior.
- `gui_harness.py` emits exactly one canonical
  `VIBECAD_GUI_HARNESS=<json>` line and uses a nested event loop with one
  absolute deadline, not sleep polling.
- controller tests retain all C00 coverage and add deterministic unit/thread
  contracts; GUI tests split non-slow parent safety from one slow real M00.

The first genuine RED is the normally collected absence of gateway lifecycle
and workbench activation behavior. The fake gate covers single-thread client
ownership, no import-time connection, idempotent activation/deactivation,
exact request mappings, one-shot review transport, plain detached events,
stale-event rejection, negative thread authority and absence of review UI.

### 67.3 Real M00 safety packet

M00 begins only after C00 is committed, pushed and the index is empty. The C01
candidate first supplies the minimal Dock and bounded harness; C01 cannot be
committed until M00 passes.

The parent must verify the canonical managed prefix receipt and runtime
generation, then bind the GUI entry and resolved target identity under that
prefix: lstat/stat device and inode, owner, mode, link count, size and
timestamps; regular file, current uid, executable and not group/world
writable. It revalidates immediately before launch and after exit.

One owner-private canonical `0700` root contains isolated `vibecad`,
`freecad-home`, `freecad-data`, `freecad-temp` and `tmp` children. The exact
GUI invocation is:

```text
<verified-prefix>/bin/FreeCAD
  -M <repo>/freecad
  -P <repo>/src
  -P <repo>/tests/fixtures/freecad_workbench
  --run-test gui_harness
```

Only the isolated VibeCAD/FreeCAD/TMP roots, verified prefix and
`PYTHONDONTWRITEBYTECODE=1` are supplied. `FreeCAD --help` is forbidden
because it is already known to enter Qt/modal startup on this host.

One campaign-wide 60-second monotonic deadline covers launch, communication,
inspection, process reclamation and daemon retirement. The GUI child starts a
new session and is immediately bound to a Darwin birth/uid/pgid/sid token. On
timeout, modal or missing result, the parent may signal the exact group only
after revalidation; identity ambiguity or replacement forbids signaling and
fails the gate.

The harness must prove exactly one addon, Workbench and Dock; reviewed source
identities; Qt binding/version; different main/worker thread identities; a
responsive heartbeat during connect/refresh; one client and exact daemon id;
and clean asynchronous deactivate, client close, thread finish and Dock
removal without a modal.

Cleanup precedes semantic acceptance on every path. It retires the
authenticated exact daemon id, proves the original process token, socket,
receipt and live run-root identity absent, and proves isolated checkout/run
roots absent or empty. The parent accepts exactly one canonical result line,
child exit zero, clean cleanup and all semantic assertions. It launches at
most once and polls the original process session.

Residuals are bounded and disclosed: no RPC cancellation beyond existing
15-second bootstrap/30-second idle bounds; exact FreeCAD 1.1 PySide namespace
and native quit order require M00; same-uid binary replacement has a narrow
check-to-exec residual; review disconnect outcome is unknown until C03
re-reads durable state. None requires a product decision or shared API change.

| Entry ID | Decision / approval | Commit / push | Gate evidence | Residual | Snapshot | State |
|---|---|---|---|---|---|---|
| MRG1-G1-C01-E00 | A04 C01/M00; sol-max read-only architecture preflight | `not-created`; forbidden before C00 close | public seam audit GO 0 blocker/major; exact 8-path/thread/mapping/test/M00 packet frozen | task pagination, RPC cancellation, real Qt/quit and binary check-to-exec bounded as above | MRG1-S21 | ready after C00 push / no product decision needed |

## 68. G1-C00 closeout and C01 execution start

G1-C00 was committed and pushed as:

```text
commit:
  15d58794b67c17794cdcb583b84be7a7c5a0cbfe
subject:
  feat(workbench): register thin-client FreeCAD addon
paths:
  exact Section 61 M+8A allowlist
staged gate:
  SG02 PASS; staged/worktree hashes identical; 19 passed; Ruff/format PASS
local/upstream:
  equal at 15d58794b67c17794cdcb583b84be7a7c5a0cbfe
index:
  empty
Release workflow push runs for exact SHA:
  0
```

Only the three standing excluded untracked paths remain outside Git. C00
therefore closes without a release-workflow failure notification and without
touching those paths.

MRG1-A04 already authorizes C01, and Section 67 found no new product decision
or shared-seam change. C01 may now start against the exact eight non-artifact
paths in Section 28.4. The implementation remains split by breakers:

1. sol-high test-first fake-host/gateway/Dock/parent-harness candidate;
2. fresh sol-max semantic/adversarial review and independent non-real
   mechanical gate;
3. one bounded real M00 launch with cleanup-before-semantics; and
4. staged-byte gate, commit and push only if every prior gate is green.

No Accept/Reject UI, preview, selection, packaging or normal-user installation
belongs to C01. Any need to modify a shared daemon/application seam, C00
`state.py`, package metadata or an unnamed path is a breaker.

| Entry ID | Decision / approval | Commit / push | Gate evidence | Residual | Snapshot | State |
|---|---|---|---|---|---|---|
| MRG1-G1-C00-E07 | A04 C00; SG02 accepted | `15d58794b67c17794cdcb583b84be7a7c5a0cbfe`; pushed | local/upstream equal; index empty; exact Release push-run count 0 | real discovery/threading remains M00 | MRG1-S22 | C00 closed |
| MRG1-G1-C01-E01 | A04 C01/M00; D00 GO | `not-created` | C00 prerequisite closed; exact 8-path allowlist; no product decision/shared seam | real GUI launch deferred until candidate passes non-real gates | MRG1-S22 | approved execution / sol-high RED-first next |

## 69. G1-C01 I01 split candidate

Two non-overlapping sol-high agents produced the eight-path C01 candidate. The
core tests first returned the required normally collected sentinel RED:

```text
test_gateway_client_lifecycle_is_owned_by_one_worker_thread
test_workbench_activation_creates_one_dock_without_blocking_main

RED:
  2 failed; absent gateway.py and host.py behavior only
identical GREEN:
  2 passed
final package + controller:
  25 passed
```

Core Ruff, format, compile and diff checks passed. The separate GUI-parent
surface returned `13 passed, 1 deselected`; its only deselected case is the
single slow M00 launch. Ruff, format, AST and diff checks also passed. No real
FreeCAD, GUI, daemon or child process ran.

The controller required a first harness correction before freezing the
candidate. It now activates the discovered Workbench through
`FreeCADGui.activateWorkbench()`, deactivates through the real Workbench
callback, uses authenticated prefix-receipt and full runtime-generation
verification, excludes mutable access time from binary identity, pre-registers
idempotent cleanup ownership and reports a proved client-connected boolean
rather than an invented count.

Frozen I01 hashes:

```text
InitGui.py:
  c8d2df3af1b3db39c57a3a40d610efd0adf4b581da4f00bcb0eb0de7ed911e4c
gateway.py:
  a293283cc923640a2f7dce0c51d7143e0fd10e85b0ea40fe78395b8d606c5194
dock.py:
  68f0f60760b216d8c5dfbfe0e45a4a191867720dd893ea836de2c381a51ba221
host.py:
  f4da6e86955164b923dd8dbf6e4910ee4c4096e904020559d27fc5bde58f571a
fake_host.py:
  d7b9e455d31d5313ad32755f7531b6c607f38ec31d50fc9a9ed1d27c38c2235b
gui_harness.py:
  48b1aac95e3eef64918a0423cc0d91f9365ba08deff412f3468fa32cd2dd44b0
controller tests:
  9c899d7c48d0b05f1db8c3a2011c4cbec5527f6916b7404831f9369497b66cdc
GUI parent tests:
  d65ff5b51d08faf6dcb255540c06fd63ab11f62fc0d8567a7bab18fbac79a9f1
```

Controller readback did not accept the candidate merely because the local
tests passed. Fresh review must specifically compare command/event field names
and types with Section 67, stable error envelopes, page/filter ownership,
inactive snapshot clearing, lifecycle closure, malformed/stale event handling,
actual refresh semantics and M00 finalizer/identity assertions. The real M00
launch remains forbidden until all blocker/major findings from that review are
closed and the full non-real mechanical gate is green.

| Entry ID | Decision / approval | Commit / push | Gate evidence | Residual | Snapshot | State |
|---|---|---|---|---|---|---|
| MRG1-G1-C01-E02 | A04 C01; split sol-high I01 | `not-created` | genuine 2-fail RED to 2-pass; core 25; GUI parent 13+1 deselected; Ruff/format/compile/AST/diff PASS | exact contract and M00 semantics require fresh adversarial review; no real launch yet | MRG1-S22 | candidate frozen / R01+M01 non-real next |

## 70. G1-C01 R01 recovery and exact wire table

Fresh sol-max R01 returned NO-GO with two blockers and five majors. It also
confirmed that the full non-real candidate is mechanically green
(`38 passed, 1 deselected`; Ruff and format pass), all nine hashes remained
frozen, and no real process ran.

### 70.1 Exact internal command/event contract

The I01 discriminator and request-id model drifted from D00. I02 is bound to
this complete exact table. Every container and key is an exact built-in type;
`schema_version` is exact integer 1 and a normal request id is an exact
non-boolean integer in `0..9007199254740991`.

```text
commands:
  connect:
    {schema_version, request_id, kind}
  list_projects / list_tasks:
    {schema_version, request_id, kind, cursor}
  refresh_project:
    {schema_version, request_id, kind, project_id}
  refresh_task:
    {schema_version, request_id, kind, task_id}
  review:
    {schema_version, request_id, kind, decision, task_id, draft_id,
     expected_generation}
  close:
    {schema_version, request_id, kind}

success events:
  connected:
    {schema_version, request_id, kind, daemon_id, worker_thread_id}
  projects / tasks / project / task / review:
    {schema_version, request_id, kind, response}
  closed:
    {schema_version, request_id, kind}

error event:
  {schema_version, request_id, kind, operation, code, outcome}
```

An error has `kind = "error"`. If no valid request id or operation can be
recovered, it uses reserved `request_id = -1` and
`operation = "invalid"`. Otherwise it preserves the validated id and kind.
The error-code set is exactly:

```text
invalid_input
unavailable
internal_error
closed
wrong_process
incompatible_kernel
```

Outcome is exactly `known_failure` or `unknown_outcome`. An authenticated
public `ok:false` mapping is a successful transport response, not an error
event. A review transport exception always yields unknown outcome, closes the
client and is never replayed or echoed.

`list_tasks` does not carry `project_id`. Gateway returns the detached
authenticated page; the Dock binds each request id to the selected project,
filters projected summaries by that project and
`status == "awaiting_user_review"`, and discards a response after the
selection context changes.

### 70.2 R01 blockers and majors

Blockers:

1. Gateway, Dock and their tests used `command`/`event`, string request ids,
   an extra task project id, `action`, extra success fields and non-contract
   error codes/keys. The complete table above must replace that self-consistent
   but incompatible protocol.
2. Host copied active worker/daemon identities into its inactive snapshot, so
   the real harness would deterministically wait until its deadline.
   Lifecycle must be exactly
   `inactive -> starting -> active -> stopping -> inactive`; connection alone
   enters active, finish clears active identities, and construction failure
   must unwind every partial Dock/thread/session without a main-thread wait.

Majors:

1. Detached signal mappings need bounded string, key, integer and per-container
   sizes in addition to the existing depth/node bounds.
2. Dock must validate exact event shapes and non-boolean schema/id values,
   turn malformed or authenticated `ok:false` projections into stable visible
   failure rather than a main-thread exception, own project/status filtering,
   clear stale task state, bind every page to selection context and implement
   both project and selected-task refresh.
3. The fake client must fail on main-thread use and fake widgets must fail on
   worker-thread access, so negative authority tests prove the intended
   boundary rather than merely documenting thread ids.
4. Real harness evidence must prove one active Dock, responsive main-thread
   heartbeat through Refresh, one actual client construction, reviewed
   InitGui/gateway/dock/host/bootstrap sources, exact prefix/home/PySide
   binding and the complete initial/starting/active/stopping/final lifecycle.
5. If GUI `Popen` succeeds and token capture fails, cleanup must never signal
   an unauthenticated pid but must boundedly wait, emit an explicit residual
   outcome and never let the action error conceal cleanup failure. With a
   token, only a freshly revalidated original session may be signaled.

I02 remains inside the approved eight-path C01 allowlist and is split into the
same non-overlapping core and harness write domains. Both sides add regression
tests first and rerun the full non-real gate. Real M00 remains forbidden until
fresh sol-max R02 and independent M02 both pass on corrected hashes.

| Entry ID | Decision / approval | Commit / push | Gate evidence | Residual | Snapshot | State |
|---|---|---|---|---|---|---|
| MRG1-G1-C01-E03 | A04 C01; sol-max R01 recovery | `not-created`; M00 forbidden | R01 NO-GO: 2 blocker / 5 major; non-real 38+1 deselected and static gates green; hashes stable | exact protocol, host teardown, boundary budgets/authority and M00 proof require I02 | MRG1-S22 | blocked / split sol-high I02 next |

## 71. G1-C01 I02 corrected candidate and dual-gate packet

The two non-overlapping sol-high correction agents closed the complete R01
recovery packet without changing the approved C01 product surface. Core I02A
first ran four targeted regressions against I01 and observed four failures.
After correction, the package and controller surface returned `34 passed`;
Ruff, format, compile and diff checks passed.

Harness I02B first ran three targeted regressions against I01 and observed
three failures: incomplete lifecycle/real Refresh evidence, absent
token-capture-failure recovery, and an action error masking cleanup failure.
After correction, the non-slow GUI-parent surface returned
`16 passed, 1 deselected`; Ruff, format, AST and diff checks passed. The one
deselected test remains the single real M00 launch. Neither correction agent
started FreeCAD, a GUI, a daemon or another child process.

I02 adds one technical-only snapshot extension needed for M00 evidence. It is
not a product feature or product-shape change. `workbench_snapshot()` is now an
exact eight-key mapping:

```text
schema_version
lifecycle
dock_count
main_thread_id
worker_thread_id
daemon_id
heartbeat_count
client_construction_count
```

`client_construction_count` is driven by successful construction in the
gateway factory, not inferred from connection state. It is zero in initial and
starting snapshots and remains cumulatively one through active, stopping and
final snapshots. `heartbeat_count` advances on worker events and the real
harness uses its delta, together with a main-thread timer delta and the emitted
Refresh command kinds, to prove observable Refresh behavior.

The corrected candidate uses the exact Section 70 command/event table, bounded
detachment budgets, Dock-owned project/status filtering and selection
contexts, main/worker negative authority in fakes, and the exact lifecycle
`inactive -> starting -> active -> stopping -> inactive`. Final inactive state
clears worker and daemon identities while retaining cumulative technical
counters. The parent harness binds the selected managed runtime generation and
GUI binary identity, permits only one launch, reserves cleanup time inside one
60-second campaign deadline, performs cleanup before semantic assertions and
never signals a child without a freshly revalidated Darwin birth token.

Frozen I02 code/test hashes:

```text
InitGui.py:
  c8d2df3af1b3db39c57a3a40d610efd0adf4b581da4f00bcb0eb0de7ed911e4c
gateway.py:
  4e8fb3542781a3768c9ac95bd26cb2bd92d5041dd940a8baf9200051e5a9cfce
dock.py:
  24889eea9605ec42c808c4bb59fb636115764c93ad30c841903679f178865524
host.py:
  70ad563c1f774c9dda8d1348dd5b3c7f26330374fb778f1a2e8e29b6a81f72a8
fake_host.py:
  1c012570b894db79ce425800c952305f922262988ac5f60748f885950e262d53
gui_harness.py:
  4cd4bfe07987020239d84f792620c2228286b251339734bdccafea56f7c2094c
controller tests:
  5edc4d104ff286ab863f72bd2e162d3a8d19386ea25760fb4aab42314b90db78
GUI parent tests:
  532ec4ea4c9d310110f16bf6fe8d7a64cc788560236d54af294fcf8d79e7fdfd
```

Fresh R02 must use sol-max and independently compare all eight frozen paths
with Sections 67 and 70, including construction-failure unwind, exact event
semantics, stale/malformed response behavior, cleanup ownership and the real
M00 assertions. Independent M02 must use terra-medium and run the combined
non-real suite, Ruff check and format check, AST/XML/diff checks, exact hashes,
allowlist status and process-residue observations. The expected combined
pytest result is `50 passed, 1 deselected`.

M00 remains forbidden unless R02 returns GO with zero blocker and zero major
and M02 returns PASS on the same nine-file snapshot. If both pass, M00 may run
exactly once. Its evidence must separately record `live capability
declarations`, `observable behavior`, `environment identity` and `public
configuration`; cleanup and zero-residue proof precede semantic acceptance.
Any real-run failure enters diagnosis/recovery without an automatic retry.

| Entry ID | Decision / approval | Commit / push | Gate evidence | Residual | Snapshot | State |
|---|---|---|---|---|---|---|
| MRG1-G1-C01-E04 | A04 C01; user authorized autonomous non-product work; split sol-high I02 | `not-created`; M00 still forbidden | core RED 4 then 34 passed; harness RED 3 then 16+1 deselected; all local static gates PASS; eight code/test hashes frozen | fresh sol-max R02 and independent terra-medium M02 required on artifact-inclusive snapshot | MRG1-S22 | corrected candidate frozen / dual non-real gates next |

## 72. G1-C01 R02 recovery and I03 packet

Independent terra-medium M02 passed on the complete nine-file I02 snapshot:
`50 passed, 1 deselected`; Ruff check, Ruff format check, AST, XML, diff and
all nine hashes passed; status, index and process observations remained
unchanged. The first report used an ambiguous artifact label and therefore
looked for a nonexistent `artifact.py`; the same agent corrected only that
path interpretation, verified
`docs/orchestrated/vibecad-multi-runtime-g1.md` at the frozen hash, did not
rerun the gates and returned PASS.

Fresh sol-max R02 nevertheless returned NO-GO with one blocker, two majors and
no minor or nit. It independently reproduced each behavioral failure while
the existing non-real suite remained green.

Blocker:

1. The real GUI harness imports `vibecad.daemon.bootstrap` before establishing
   repository-source precedence. Section 32 already proved that FreeCAD `-P`
   appends paths, and the selected managed prefix contains an installed
   `vibecad`. M00 would therefore load installed code before the reviewed
   repository source and fail the parent source-identity assertions; the
   gateway could also use an installed stale client. I03 must, before the
   first `vibecad` import, derive repository `src` from the canonical harness
   `__file__`, reject any preloaded `vibecad` namespace, remove duplicate
   spellings, place the repository path at exact `sys.path[0]`, invalidate
   import caches, import and immediately verify `vibecad` plus bootstrap
   source identities. A non-real regression must simulate an installed path
   ahead of repository source and prove the correction.

Majors:

1. If session construction fails after the worker thread has started, Host
   removes the Dock and calls `thread.quit()` without a worker-owned gateway
   close. R02 reproduced an emitted connect followed by failure and observed
   one constructed client with no close. I03 must queue an exact close on the
   worker, let the `closed` event drive thread quit, never close or wait on the
   main thread, and prove async final inactive state, zero Dock and exact-once
   client close for failures before and after worker start.
2. Task pagination is bound only to project and selection epoch. Starting a
   second scan for the same selected project leaves the first scan's pages
   acceptable; R02 reproduced a stale task entering the selector. I03 must add
   a monotonically distinct task-load epoch for every `cursor=None` scan, bind
   all continuation pages to it and accept/update/continue only the latest
   epoch. It must also remove the explicit `_project_changed(0)` call after
   `QComboBox.setCurrentIndex(0)`, because real Qt already emits the index
   signal, and prove one initial task scan plus same-selection/interleaved-page
   stale rejection.

I03 remains inside the approved C01 product surface. The core write domain is
`host.py`, `dock.py`, the fake host and controller tests. The harness write
domain is `gui_harness.py` and the GUI-parent test. `InitGui.py` and
`gateway.py` remain frozen unless a new reviewed recovery packet proves a need
to change them. Both sol-high correction agents must add regression tests
first, observe RED on I02, then implement and run their complete non-real
domain gates. They may not start real FreeCAD, a GUI, a daemon or another
child process.

After I03 freezes, both independent gates restart on new hashes: sol-max R03
must return GO with zero blocker and zero major, and terra-medium M03 must
return PASS. M00 remains forbidden until both results hold on the same
snapshot. No automatic real-run retry is authorized by this recovery packet.

| Entry ID | Decision / approval | Commit / push | Gate evidence | Residual | Snapshot | State |
|---|---|---|---|---|---|---|
| MRG1-G1-C01-E05 | A04 C01; autonomous technical recovery | `not-created`; M00 forbidden | M02 PASS: 50+1 deselected and all static/hash/status gates; R02 NO-GO: 1 blocker / 2 major | repository-source precedence, worker-owned partial unwind and task-load epoch require split sol-high I03 | MRG1-S22 | blocked / I03 RED-first next |

## 73. G1-C01 I03 corrected candidate and restarted dual gates

The split sol-high I03 agents stayed inside their non-overlapping write
domains and added regression tests before implementation.

Core I03 observed three targeted failures on I02: the initial project load
started two task scans, an old same-selection continuation was accepted after
a new scan, and a post-connect `Dock.start()` failure left the constructed
client unclosed. The corrected targeted set returned `4 passed`; the complete
package and controller surface returned `36 passed`. Ruff, format, AST and
diff checks passed.

Harness I03 observed five targeted failures on I02 because the repository
binding operation did not exist. The identical targeted selection then
returned `5 passed, 17 deselected`; the complete non-slow GUI-parent surface
returned `21 passed, 1 deselected`. Ruff, format, AST and diff checks passed.
Neither agent started FreeCAD, a GUI, a daemon, `Popen`, network work or an
installer.

Host now uses a session-owned queued close signal for a partial failure after
worker-thread start. The close command is processed by the same worker that
owns the gateway/client, and only an exact three-key `closed` event with the
reserved recovery request id drives thread quit. The main thread neither
closes the client nor waits. Regression evidence proves final inactive
eight-key state, zero Dock, one construction, exact-once close on the
construction thread, thread finish and global-session clearing. A failure
before thread start retains the direct inactive unwind.

Dock now advances a distinct task-load epoch for every first page and binds
each continuation to `(project_id, selection_epoch, task_load_epoch)`.
Responses can update or continue only the current triple. Project population
sets the selector through one real signal transition and no longer calls
`_project_changed()` explicitly; the fake selector now models first-item,
clear and same-index signal behavior closely enough to prove one initial scan.

The real GUI harness now performs repository binding as the first operation in
the nested probe, before any `vibecad` import. It derives canonical repository
and `src` identities from its own reviewed source path, verifies regular source
files, rejects every preloaded `vibecad` namespace, removes equivalent
repository-source spellings, inserts the canonical source at exact
`sys.path[0]`, invalidates caches, imports the package and bootstrap in order
and immediately verifies both source identities. The binding is delayed until
probe execution, so ordinary parent-side module inspection remains inert. M00
now also reports and the parent asserts `vibecad_source`.

Frozen I03 code/test hashes:

```text
InitGui.py:
  c8d2df3af1b3db39c57a3a40d610efd0adf4b581da4f00bcb0eb0de7ed911e4c
gateway.py:
  4e8fb3542781a3768c9ac95bd26cb2bd92d5041dd940a8baf9200051e5a9cfce
dock.py:
  239790327fc3f59fb081171201a4c3dd349e6b4be324165737adea11e6fd10b4
host.py:
  6ae1e148d2684b4d1a644d8f0bc280572aa5bf7d05caf86361f589c92ae18330
fake_host.py:
  0bfa6a6b05f5aec03a94f664ea3d9988fb48992efeca8e3213a58253da087e3b
gui_harness.py:
  c7006d9ffa611aa66a513f55a7ce6a4cd7f489baf4b0e05b417efe12f36740f9
controller tests:
  c12eabd2d40d930d0efd438643d1c9d375baf5b15ffefff02d83afd03de5ab27
GUI parent tests:
  5b429272e324f06c82290b02d06a7c6fa7661948d13ac0c0274f0552a4353e75
```

The complete non-real expected result is now `57 passed, 1 deselected`.
Independent R03 restarts with sol-max and must recheck all R01/R02 findings,
the exact wire contract and the real-M00 proof. Independent M03 restarts with
terra-medium and must run the combined suite plus static/hash/status/process
gates. Both use the same artifact-inclusive nine-file snapshot. M00 remains
forbidden until R03 is GO with zero blocker and zero major and M03 is PASS.

| Entry ID | Decision / approval | Commit / push | Gate evidence | Residual | Snapshot | State |
|---|---|---|---|---|---|---|
| MRG1-G1-C01-E06 | A04 C01; split sol-high I03 | `not-created`; M00 still forbidden | core RED 3 then target 4/full 36; harness RED 5 then target 5/full 21+1 deselected; all local static gates PASS | fresh sol-max R03 and independent terra-medium M03 required on new artifact-inclusive snapshot | MRG1-S22 | I03 candidate frozen / dual gates restarted |

## 74. G1-C01 M00 pre-launch failure and D01 recovery

The I03 dual gates passed on the same frozen snapshot. Terra-medium M03
returned PASS with `57 passed, 1 deselected`, Ruff, format, AST, XML, diff,
nine hashes, unchanged Git/index state and no real process. Sol-max R03
returned GO with zero blocker, zero major, zero minor and zero nit after
rechecking every R01/R02 finding and the M00 recovery semantics.

The subsequently authorized slow test invocation failed in 0.42 seconds at
the first full managed-runtime authentication. It failed before the isolated
M00 root, `Popen`, process-token capture or `launch_count` were created.
Consequently the real FreeCAD GUI launch count remains zero. Immediate
observation found no FreeCAD or daemon process, no
`/private/tmp/vc-g1m00-*` directory, no Git/index drift and no changed
candidate hash. Per the recovery rule, the test was not rerun.

Independent sol-max D01 proved a deterministic test-harness environment
contract conflict:

1. M00 requires `VIBECAD_FREECAD_ENV` in order to select the explicit managed
   prefix.
2. `verify_runtime_generation()` prepares private FreeCAD process directories
   before spawning its evidence-bound verification child.
3. That preparation treats any `VIBECAD_FREECAD_ENV` value as an external
   override and correctly rejects overlap between an external prefix and the
   private VibeCAD runtime tree.
4. The selected canonical managed prefix is inside that runtime tree, so the
   M00 test's temporary selection variable triggers the external-overlap
   defense. The resulting `ValueError` is conservatively returned as
   verification false before a verification child can spawn.

The same generation evidence with the variable present returned
`verify=false`, zero verification spawns and unchanged generation. With only
that variable removed, the exact full verification spawned once, returned
zero with empty stdout/stderr, returned `verify=true` and recaptured the same
generation. The in-prefix receipt is exact current and has SHA-256
`b154e2189adaf718a9231aef30972e25774e20d4d888aa5f4e95520793d64fbd`.
Python 3.12, FreeCAD 1.1.0, installed VibeCAD 0.6.0, server epoch 4, MCP
1.27.2 and the 28-tool public-surface digest all match the current contract.
This is neither engine drift, stale server code, a false-current receipt nor
host damage.

I04 is test-only and does not alter product behavior or weaken the production
external-overlap defense. Its sole implementation path is
`tests/test_freecad_workbench_gui.py`. After resolving and binding the
canonical managed prefix, the M00 helper must remove
`VIBECAD_FREECAD_ENV` only for the evidence capture/full authentication
interval, restore the exact original value in a `finally` path, and only then
resolve the FreeCAD binary and construct the real GUI environment. Regression
tests must first fail on I03, then prove removal during authentication,
exact restoration on success and failure, prefix/evidence binding, and
continued rejection of a genuine external-overlap case.

I04 recovery gates are:

1. sol-high RED-first implementation and the complete non-slow GUI-parent
   suite;
2. non-slow `tests/test_status.py` together with the complete C01 combined
   suite, Ruff, format, AST/XML/diff and new frozen hashes;
3. a terra-medium bounded stability gate of three consecutive full managed
   verifications, each with exactly one child spawn, return code zero, exact
   receipt and unchanged recaptured generation;
4. fresh sol-max R04 with zero blocker and zero major plus independent
   terra-medium M04 PASS on the same artifact-inclusive snapshot;
5. zero FreeCAD/daemon/process/temp-root residue before and after all non-real
   recovery gates.

Only after all five hold may one new M00 launch be authorized. The pre-launch
failure did not consume the real GUI launch budget, but it cannot be treated
as an automatic retry.

| Entry ID | Decision / approval | Commit / push | Gate evidence | Residual | Snapshot | State |
|---|---|---|---|---|---|---|
| MRG1-G1-C01-E07 | A04 C01; R03/M03 dual release; autonomous test-only recovery | `not-created`; GUI launch count 0 | R03 GO 0/0/0/0; M03 PASS 57+1; slow invocation failed before Popen; D01 deterministic managed-selection/external-overlap conflict; zero residue | I04 RED-first harness fix, stability gate and fresh R04/M04 required before one new launch | MRG1-S22 | recovery / M00 rerun forbidden |

## 75. G1-C01 I04 managed-authentication correction

The sol-high I04 agent modified only
`tests/test_freecad_workbench_gui.py`. Five new non-real regressions first
returned four failures because the managed-authentication helper was absent;
the genuine external-overlap defense already passed. The identical targeted
selection then returned `5 passed`.

The new helper accepts only the canonical managed prefix, dynamically resolves
its dependencies so monkeypatching remains authoritative, removes
`VIBECAD_FREECAD_ENV` only across generation capture and full authentication,
binds both captured and authenticated evidence to the selected prefix, and
restores the exact original environment mapping in `finally` on success or
failure. Authentication errors are not swallowed. M00 resolves the canonical
prefix from the original selection, uses the helper, explicitly confirms the
selection has been restored, and only then resolves the FreeCAD executable.
No production runtime or external-overlap code changed.

Post-format evidence:

```text
targeted I04:
  5 passed
GUI parent, non-slow:
  26 passed, 1 deselected
C01 package + controller + GUI, non-slow:
  62 passed, 1 deselected
status + C01 package + controller + GUI, non-slow:
  139 passed, 1 deselected
Ruff / format / AST / XML / diff:
  PASS
```

The only changed I03 code/test hash is:

```text
tests/test_freecad_workbench_gui.py:
  98c8c6d547a17e28f3f1067f315a5300ca464ab3105fb39692d81e0fe110330e
```

All other Section 73 code/test hashes remain frozen. I04 did not run slow,
FreeCAD, a GUI, a daemon, `Popen`, an installer, network work or a real managed
verification child. The index remained empty and HEAD/upstream did not move.

The artifact-inclusive I04 snapshot now restarts R04 and M04. M04 must include
the bounded three-run full managed verification stability gate with the
selection variable removed: each run must observe exact receipt, one
evidence-bound child spawn, return code zero, unchanged generation and no
FreeCAD/daemon/temp-root residue. It must also repeat the `62 passed,
1 deselected` C01 gate, the `139 passed, 1 deselected` status-inclusive gate
and all static/hash/status checks. R04 must independently prove that the helper
does not turn a genuine external prefix into a managed one, restores every
environment path and preserves all previously closed C01 findings.

One new real GUI launch remains forbidden until M04 is PASS and R04 is GO with
zero blocker and zero major on the same snapshot.

| Entry ID | Decision / approval | Commit / push | Gate evidence | Residual | Snapshot | State |
|---|---|---|---|---|---|---|
| MRG1-G1-C01-E08 | A04 C01; sol-high I04 test-only recovery | `not-created`; GUI launch count 0 | RED 4/1 existing defense pass; targeted 5; GUI 26+1; C01 62+1; status-inclusive 139+1; static gates PASS | three-run stability, fresh sol-max R04 and terra-medium M04 required | MRG1-S22 | I04 frozen / new launch forbidden |

## 76. G1-C01 R04 recovery and I05 packet

Terra-medium M04 passed the complete I04 snapshot. Its three consecutive full
managed verifications each observed exact receipt, one evidence-bound child
spawn, return code zero, empty stdout/stderr, verification true and an exactly
equal fresh recapture; the generation was identical across all three runs.
The `62 passed, 1 deselected` C01 gate, `139 passed, 1 deselected`
status-inclusive gate, static checks, nine hashes, Git state and zero
FreeCAD/daemon/temp-root observations also passed.

Fresh sol-max R04 returned NO-GO with zero blocker, three majors and no minor
or nit:

1. The helper compared resolved managed prefixes but restored an unvalidated
   original `VIBECAD_FREECAD_ENV` spelling. An alias or `..` spelling could
   therefore authenticate the canonical prefix, be restored exactly and then
   drive `paths.freecad_path()` with a noncanonical launch spelling. I05 must
   require the selection's exact built-in string value to equal
   `str(canonical_prefix)` before removing it; absent, aliased or other-typed
   values fail before capture.
2. `_authenticate_runtime_generation()` retained definition-time defaults for
   receipt read, capture and verification. A monkeypatched verifier was not
   used by the default path. I05 must use `None` defaults and dynamically
   resolve all status dependencies at call time.
3. The helper checked only the authenticated evidence prefix, so an injected
   authentication result with the same prefix but different identities was
   accepted. I05 must require exact `RuntimeGenerationEvidence` type and exact
   equality between authenticated and captured generations in addition to the
   canonical prefix binding.

I05 remains a one-file, test-only sol-high correction in
`tests/test_freecad_workbench_gui.py`. It must add targeted tests first and
observe all three omissions on I04. The tests must also retain success/failure
environment restoration, the genuine external-overlap defense and M00's
post-helper `freecad_path()` ordering.

After I05 freezes, M05 repeats the complete non-real suites, all static/hash
checks and the bounded three-verification stability gate. Fresh sol-max R05
must return zero blocker and zero major. One new GUI launch remains forbidden
until both pass on the same artifact-inclusive snapshot.

| Entry ID | Decision / approval | Commit / push | Gate evidence | Residual | Snapshot | State |
|---|---|---|---|---|---|---|
| MRG1-G1-C01-E09 | A04 C01; autonomous test-only hardening | `not-created`; GUI launch count 0 | M04 PASS including three stable verifies; R04 NO-GO 0 blocker / 3 major | canonical selection spelling, dynamic defaults and exact generation equality require I05 | MRG1-S22 | blocked / sol-high I05 RED-first |

## 77. G1-C01 I05 exact-selection and evidence hardening

The sol-high I05 correction remained in the single
`tests/test_freecad_workbench_gui.py` write domain. Its new targeted selection
first returned `11 failed, 1 passed, 27 deselected`. The failures covered the
definition-time dependency capture, seven absent/aliased/non-exact selection
forms, non-exact captured evidence type, non-exact authenticated evidence type
and a same-prefix/different-generation authentication result. The already
correct helper/restoration/`freecad_path()` ordering test passed.

I05 now:

1. requires `VIBECAD_FREECAD_ENV` to be present, an exact built-in string and
   exactly `str(canonical_managed_prefix)` before capture or environment
   mutation;
2. resolves receipt-read, capture and verify dependencies dynamically at each
   `_authenticate_runtime_generation()` call;
3. requires exact `RuntimeGenerationEvidence` type at every boundary and exact
   equality between captured and authenticated generations;
4. repeats the exact canonical selection check in the M00 body before calling
   the helper, then confirms exact restoration before `freecad_path()`.

Post-format frozen evidence:

```text
I05 targeted plus retained I04 recovery tests:
  17 passed, 22 deselected
GUI parent, non-slow:
  38 passed, 1 deselected
C01 package + controller + GUI, non-slow:
  74 passed, 1 deselected
status + C01 package + controller + GUI, non-slow:
  151 passed, 1 deselected
Ruff / format / AST / XML / diff:
  PASS
```

The new GUI-parent test SHA-256 is:

```text
1043c69248fc367f7718df16b0dd0979bf465732643e7cb81ef948637be6d150
```

All other Section 73 code/test hashes remain frozen. No slow, real verifier,
FreeCAD, GUI, daemon, `Popen`, installer or network operation ran; the index
remained empty.

Fresh R05 and M05 restart on the artifact-inclusive I05 snapshot. M05 repeats
the `74 passed, 1 deselected` and `151 passed, 1 deselected` suites, static and
hash gates, zero-residue observations and the complete three-run managed
verification stability gate. R05 must adversarially reproduce the three R04
attacks and return zero blocker and zero major. One new GUI launch remains
forbidden until both pass.

| Entry ID | Decision / approval | Commit / push | Gate evidence | Residual | Snapshot | State |
|---|---|---|---|---|---|---|
| MRG1-G1-C01-E10 | A04 C01; sol-high I05 test-only hardening | `not-created`; GUI launch count 0 | RED 11/1; targeted 17; GUI 38+1; C01 74+1; status-inclusive 151+1; static gates PASS | fresh sol-max R05 and terra-medium M05 including three stable verifies | MRG1-S22 | I05 frozen / launch forbidden |

## 78. G1-C01 first real GUI result and D02 recovery packet

Fresh sol-max R05 returned GO with zero blocker, zero major, zero minor and
zero nit. Independent terra-medium M05 passed the complete I05 snapshot. Its
three consecutive managed-runtime verifications each observed the exact
receipt, exactly one evidence-bound verification child, return code zero,
empty stdout/stderr, verification true and an exactly equal fresh generation
recapture. The `74 passed, 1 deselected` C01 gate, `151 passed, 1 deselected`
status-inclusive gate, static checks, nine frozen hashes, Git/index state and
zero-residue observations also passed.

The then-admitted single real GUI launch ran once and failed after
approximately 41.86 seconds. The GUI child exited naturally, the managed
runtime and executable identity rechecks passed, but the daemon cleanup guard
returned `publication_unproven`. Cleanup-first handling correctly prevented
semantic validation. It also raised before exposing the already captured child
stdout/stderr, leaving insufficient bounded evidence for direct diagnosis.
The launch was not retried. Immediate and repeated observations found no
FreeCAD, FreeCADCmd or `vibecad.daemon` process, no
`/private/tmp/vc-g1m00-*` root, no daemon receipt or socket, no index drift and
no candidate-file drift.

Read-only sol-max D02 located a high-confidence static root-cause chain:

1. The admitted runtime contains FreeCAD 1.1.0 with PySide6 6.10.2. Its
   generated interface exposes `Qt.ConnectionType.QueuedConnection` and
   `Qt.DockWidgetArea.RightDockWidgetArea`.
2. `host.py` instead reads the legacy flat `Qt.QueuedConnection` four times
   and `Qt.RightDockWidgetArea` once.
3. The fake Qt host exposes only those flat attributes, so every non-real
   controller test reproduced the legacy shape and concealed the real PySide6
   incompatibility.
4. The first flat lookup occurs before `thread.start()` and `dock.start()`.
   Session construction catches the resulting exception, disposes the
   unstarted objects and returns to `inactive`.
5. The GUI harness activation loop does not treat `inactive`, `stopping` or a
   Dock status of `Unavailable` as a terminal activation failure. It therefore
   waits near its absolute deadline, then exits without a gateway dispatch or
   an authenticated daemon publication. The parent consequently observes
   `publication_unproven`.

`publication_unproven` proves only that the parent could not authenticate a
publication when it observed the run root. It does not by itself prove that a
daemon never spawned. A healthy daemon would remain available after the GUI
session, however, so a healthy publication being merely missed is unlikely.
D02 ranked the remaining diagnostic candidates below the enum mismatch:

1. exact GUI program spelling versus the daemon bootstrap's exact
   `Py_GetProgramFullPath` / `sys.executable` admission;
2. the GUI test's seven-variable environment versus the inherited C00B probe
   environment and its `QT_QPA_PLATFORM=offscreen`;
3. a transient daemon publication followed by an early crash;
4. run-root, repository-import or unittest-loader disagreement.

I06 is a technical recovery within A04 and does not change product scope or
shape. Its sol-high write domain is limited to `host.py`, the fake host, the
GUI harness and the controller/GUI test files. It must first obtain non-real
RED evidence, then:

1. use the PySide6 nested enum members with a narrowly tested Qt5 flat fallback;
2. exercise a nested-only fake Qt shape so the real binding contract cannot be
   concealed again;
3. make activation fail fast on `inactive`, `stopping` or `Unavailable`, with
   a bounded last snapshot and Dock status;
4. preserve cleanup-first ownership while always attempting one bounded parent
   evidence emission after cleanup and before cleanup/action/semantic
   assertions;
5. cap child stdout/stderr tails at 2,000 characters and prohibit all child
   text from participating in process, receipt, cleanup or signal authority;
6. prove cleanup executes only once across action, cleanup, evidence-emission
   and finalizer error combinations.

After I06 freezes, a fresh sol-max adversarial review and independent
terra-medium mechanical gate must both pass on the same artifact-inclusive
snapshot. They must include the complete non-slow C01 and status suites,
managed verification stability, static/hash/Git checks and zero-residue
observations. No second real GUI launch is admitted until both gates explicitly
confirm every I06 recovery invariant and the cold-start preflight is clean.

| Entry ID | Decision / approval | Commit / push | Gate evidence | Residual | Snapshot | State |
|---|---|---|---|---|---|---|
| MRG1-G1-C01-E11 | A04 C01; autonomous technical recovery; first real launch consumed | `not-created`; GUI launch count 1 | R05 GO 0/0/0/0; M05 PASS; one real child natural exit; cleanup `publication_unproven`; D02 high-confidence PySide6 enum chain; zero residue | I06 RED-first compatibility, fail-fast and bounded cleanup-first evidence; fresh dual gates | MRG1-S22 | recovery / new launch forbidden |

## 79. G1-C01 I06 PySide6 and diagnostic recovery

The sol-high I06 agent remained inside the five-path recovery write domain:

```text
freecad/VibeCAD/vibecad_workbench/host.py
tests/fixtures/freecad_workbench/fake_host.py
tests/fixtures/freecad_workbench/gui_harness.py
tests/test_freecad_workbench_controller.py
tests/test_freecad_workbench_gui.py
```

The same targeted selection first returned `6 failed`, covering the
nested-only PySide6 enum shape, three activation terminal states and two
cleanup/evidence lifecycle cases. After implementation it returned
`6 passed`.

I06 now:

1. resolves `QueuedConnection` and `RightDockWidgetArea` from the PySide6
   nested enum namespaces, with a narrow legacy flat fallback when a nested
   member is unavailable;
2. provides a nested-only fake Qt shape and proves a complete
   activate/deactivate lifecycle against it;
3. treats `inactive`, `stopping` and Dock `Unavailable` as terminal while the
   GUI harness is activating, emits a bounded diagnostic containing the last
   snapshot and Dock status, and quits the nested loop instead of waiting for
   the absolute deadline;
4. captures the GUI action result, executes and caches cleanup exactly once,
   attempts exactly one bounded parent-evidence emission, and only then raises
   cleanup, action, evidence or semantic failures;
5. retains at most 2,000 characters of each child output stream and extracts
   only bounded status/error diagnostics; no child text participates in
   daemon receipt, process-token, cleanup or signal authority;
6. combines action, cleanup and evidence-emission errors without allowing the
   finalizer to re-enter cleanup.

Post-format non-real evidence:

```text
I06 targeted:
  RED 6 failed
  GREEN 6 passed
C01 package + controller + GUI, non-slow:
  80 passed, 1 deselected
status + C01 package + controller + GUI, non-slow:
  157 passed, 1 deselected
Ruff / format / AST / XML / diff:
  PASS
```

The code/test snapshot before this artifact entry was:

```text
freecad/VibeCAD/InitGui.py:
  c8d2df3af1b3db39c57a3a40d610efd0adf4b581da4f00bcb0eb0de7ed911e4c
freecad/VibeCAD/vibecad_workbench/gateway.py:
  4e8fb3542781a3768c9ac95bd26cb2bd92d5041dd940a8baf9200051e5a9cfce
freecad/VibeCAD/vibecad_workbench/dock.py:
  239790327fc3f59fb081171201a4c3dd349e6b4be324165737adea11e6fd10b4
freecad/VibeCAD/vibecad_workbench/host.py:
  c81b6e5759ef35aa454a8f642d5ed61ee595594a1451804b49924aa775f344c2
tests/fixtures/freecad_workbench/fake_host.py:
  528a61ec9b0e1a4c7500466ce6fd8d38fd63cb222c86f3f793782dbded409a6c
tests/fixtures/freecad_workbench/gui_harness.py:
  9e00c58c6075599d33d2e822e3e7b108bb503c90dc04d720f98d82c11efcb409
tests/test_freecad_workbench_controller.py:
  5059107de207a5caef649705186334032c8626bcb708d61d6323ec9bf457e5ec
tests/test_freecad_workbench_gui.py:
  7ed57a99d7ca595d37231bee6e001ae1be20023bb22b806315080918e6a47f7a
```

I06 did not run a slow test, real verifier, FreeCAD, GUI, daemon, installer or
network operation. HEAD and upstream remain
`15d58794b67c17794cdcb583b84be7a7c5a0cbfe`, the index remains empty and
the excluded untracked paths remain outside the campaign.

Fresh sol-max R06 and independent terra-medium M06 now restart on the
artifact-inclusive snapshot. M06 must repeat the complete non-real suites,
three consecutive managed full verifications, static/hash/Git checks and
zero-residue observations. R06 must adversarially attack the nested/legacy
enum resolver, activation terminal detection, bounded evidence, cleanup
ownership and the separation between untrusted child output and cleanup
authority. A new real GUI launch remains forbidden until both gates pass on
the same frozen snapshot.

| Entry ID | Decision / approval | Commit / push | Gate evidence | Residual | Snapshot | State |
|---|---|---|---|---|---|---|
| MRG1-G1-C01-E12 | A04 C01; sol-high I06 technical recovery | `not-created`; GUI launch count 1 | RED 6; targeted 6; C01 80+1; status-inclusive 157+1; static gates PASS; zero residue | fresh sol-max R06 and terra-medium M06 including three stable managed verifies | MRG1-S22 | I06 frozen / new launch forbidden |

## 80. G1-C01 R06 recovery and I07 packet

Fresh sol-max R06 returned NO-GO with zero blocker, two majors, one minor and
zero nit. The I06 targeted selection, the `80 passed, 1 deselected` C01 gate,
the `157 passed, 1 deselected` status-inclusive gate and Ruff, format, AST,
XML and diff checks all passed. An independent eight-case enum attack matrix
also passed nested-member preference, legacy flat fallback, missing or `None`
nested namespaces/members, `AttributeError` fallback, non-attribute descriptor
failure closure and failed-import cleanup. Those passes did not close three
recovery-contract gaps:

1. **Major:** `_cleanup_before_semantics()` parsed untrusted child stdout
   before calling `cleanup_once()`. The observed event order was
   `action -> parse -> cleanup -> evidence`. A large or adversarial result can
   therefore consume the cleanup reserve before authenticated cleanup begins.
   I07 must require `action -> cleanup -> parse -> evidence`; parse errors
   remain bounded evidence and must not delay, authorize or suppress cleanup.
2. **Major:** the M00 action created a populated `_ParentAttempt`, then
   performed publication, executable-identity and runtime-generation checks.
   A post-capture exception escaped the action, causing the outer lifecycle to
   replace the populated attempt with an empty action-error attempt. R06
   reproduced parent evidence with a `None` return code and empty streams even
   though the child result had already been captured. I07 must attach each
   post-capture error to an attempt that preserves return code, timeout and
   both captured streams.
3. **Minor:** the artifact requires a narrowly tested Qt5 flat fallback, but
   the default fake Qt exposes both nested and flat members and the new
   nested-only test exercises only the PySide6 branch. The implementation's
   fallback passed R06's synthetic attack, but the frozen regression suite
   never executes it. I07 must add a flat-only fake and a complete
   activate/deactivate regression.

I07 remains a non-product, sol-high recovery. Its write domain is limited to
the fake host, controller tests and GUI-parent tests. It must obtain RED
evidence for all three gaps, then repeat the complete non-slow and static
gates. No managed full verification or M00 launch is admitted while I07 is
mutable. After I07 freezes, both R07 and M07 restart on its
artifact-inclusive snapshot; earlier R06/M05 results cannot release it.

R06 observed no candidate/hash/Git/index drift, real process or M00 temporary
root. The terra-medium M06 attempt was not run: agent creation was refused by
the current subagent thread limit, and the main agent did not substitute a
different model or self-execute the mechanical release gate.

| Entry ID | Decision / approval | Commit / push | Gate evidence | Residual | Snapshot | State |
|---|---|---|---|---|---|---|
| MRG1-G1-C01-E13 | A04 C01; autonomous technical hardening | `not-created`; GUI launch count 1 | R06 NO-GO 0 blocker / 2 major / 1 minor / 0 nit; non-real suites/static gates PASS; zero residue; M06 not run due agent limit | cleanup-before-parse, preserve post-capture evidence, explicit flat-only regression | MRG1-S22 | blocked / sol-high I07 RED-first |

## 81. G1-C01 I07 strict cleanup-first recovery

The sol-high I07 correction modified only the three expanded recovery paths:

```text
tests/fixtures/freecad_workbench/fake_host.py
tests/test_freecad_workbench_controller.py
tests/test_freecad_workbench_gui.py
```

The same targeted selection first returned `5 failed`: one strict
cleanup-before-parse ordering failure, three post-capture preservation cases
and one missing flat-only Qt5 lifecycle case. After implementation it returned
`5 passed`.

I07 now:

1. calls `cleanup_once()` before parsing or otherwise inspecting child stdout;
   only after cleanup has returned does it parse bounded diagnostic fields,
   attempt exactly one evidence emission and then assert cleanup/action/evidence
   or semantic outcomes;
2. uses a non-real `_finalize_captured_attempt()` helper for post-capture
   publication and runtime/executable rechecks. A failing recheck is attached
   to a replacement attempt that preserves the original return code, timeout,
   stdout and stderr;
3. retains the existing best-effort policy for an unobserved publication
   represented by `RuntimeError`, continues the post-capture recheck, and
   leaves authenticated cleanup to the cleanup guard;
4. adds a mutually exclusive flat-only fake Qt shape and proves a full
   activate/deactivate lifecycle through the Qt5 compatibility branch.

Post-format non-real evidence:

```text
I07 targeted:
  RED 5 failed
  GREEN 5 passed
C01 package + controller + GUI, non-slow:
  85 passed, 1 deselected
status + C01 package + controller + GUI, non-slow:
  162 passed, 1 deselected
Ruff / format / AST / XML / diff:
  PASS
```

The code/test snapshot before this artifact entry was:

```text
freecad/VibeCAD/InitGui.py:
  c8d2df3af1b3db39c57a3a40d610efd0adf4b581da4f00bcb0eb0de7ed911e4c
freecad/VibeCAD/vibecad_workbench/gateway.py:
  4e8fb3542781a3768c9ac95bd26cb2bd92d5041dd940a8baf9200051e5a9cfce
freecad/VibeCAD/vibecad_workbench/dock.py:
  239790327fc3f59fb081171201a4c3dd349e6b4be324165737adea11e6fd10b4
freecad/VibeCAD/vibecad_workbench/host.py:
  c81b6e5759ef35aa454a8f642d5ed61ee595594a1451804b49924aa775f344c2
tests/fixtures/freecad_workbench/fake_host.py:
  ba75447c5887016064c2439acb9e43044d11f38e93b89e6a92c810814f9d2fd4
tests/fixtures/freecad_workbench/gui_harness.py:
  9e00c58c6075599d33d2e822e3e7b108bb503c90dc04d720f98d82c11efcb409
tests/test_freecad_workbench_controller.py:
  75144c6b08314df4b22b5aef999e387c5b83f9f4282d2b7d0380288dab0252c0
tests/test_freecad_workbench_gui.py:
  bebde58d58416feae1004ef4c854b94cabbcd09266d99a115e4c5d76352366d3
```

I07 ran no slow test, managed verifier, FreeCAD, GUI, daemon, `Popen`,
installer or network operation. HEAD/upstream remain
`15d58794b67c17794cdcb583b84be7a7c5a0cbfe`; the index is empty, the exact
campaign status is unchanged and no real process or M00 temporary root was
observed.

Fresh sol-max R07 and independent terra-medium M07 restart on this
artifact-inclusive snapshot. R07 must reproduce every R06 finding and attack
strict cleanup-before-parse, preserved post-capture errors, publication
best-effort handling and the explicit flat-only lifecycle. M07 must repeat all
non-real suites, static/hash/Git checks, zero-residue observations and three
consecutive managed full verifications. One new real GUI launch remains
forbidden until both gates pass the identical frozen candidate and a final
cold-start preflight is clean.

| Entry ID | Decision / approval | Commit / push | Gate evidence | Residual | Snapshot | State |
|---|---|---|---|---|---|---|
| MRG1-G1-C01-E14 | A04 C01; sol-high I07 strict cleanup-first recovery | `not-created`; GUI launch count 1 | RED 5; targeted 5; C01 85+1; status-inclusive 162+1; static gates PASS; zero residue | fresh sol-max R07 and terra-medium M07 including three stable managed verifies | MRG1-S22 | I07 frozen / new launch forbidden |

## 82. G1-C01 R07/M07 and I08 publication fail-closed recovery

Independent terra-medium M07 passed the complete I07 snapshot. Three
consecutive managed full verifications each observed the exact receipt,
exactly one verification child, return code zero, empty stdout/stderr,
verification true and an exactly equal fresh generation recapture; all three
generations were equal. The I07 targeted selection returned `5 passed`, C01
returned `85 passed, 1 deselected`, the status-inclusive gate returned
`162 passed, 1 deselected`, and all static/hash/Git/status/zero-residue checks
passed.

Sol-max R07 reproduced and closed all two majors and one minor from R06, but
found one new major in `_finalize_captured_attempt()`. I07 treated every
`RuntimeError` from `observe_publication()` as the best-effort “not observed”
case. The actual cleanup guard represents an unobserved publication by a
normal `None` return. Its `RuntimeError` cases instead include a missing cold
proof, ambiguous publication, invalid authenticated receipt and a publication
that changed after authentication. Swallowing those errors could therefore
conceal a daemon-identity or generation failure. R07 was NO-GO with zero
blocker, one major, zero minor and zero nit. Once I08 changed the snapshot,
the old R07 run was stopped rather than completing redundant release checks;
M07's PASS remains historical evidence and cannot release I08.

The sol-high I08 correction modified only
`tests/test_freecad_workbench_gui.py`. Its targeted selection first returned
`5 failed, 2 passed`: four parameterized authenticated-publication
`RuntimeError` cases and one cleanup/evidence integration case failed, while a
normal `None` result and an already fail-closed non-`RuntimeError` case passed.
The same selection then returned `7 passed`.

I08 now:

1. continues post-capture rechecks only when publication observation returns
   normally, including the no-publication `None` result;
2. attaches every publication `RuntimeError` or other `BaseException` to a
   replacement attempt preserving return code, timeout and both child streams;
3. does not run the later recheck after a publication-observation exception;
4. still performs cleanup exactly once and emits bounded evidence containing
   the preserved child fields and publication error.

Post-format non-real evidence:

```text
I08 targeted:
  RED 5 failed, 2 passed
  GREEN 7 passed
C01 package + controller + GUI, non-slow:
  91 passed, 1 deselected
status + C01 package + controller + GUI, non-slow:
  168 passed, 1 deselected
Ruff / format / AST / XML / diff:
  PASS
```

The only I07 code/test hash changed by I08 is:

```text
tests/test_freecad_workbench_gui.py:
  dafc66c806a2a5ce0271f834080c922bcfb6f789f182c30584554da95de1acb3
```

All other Section 81 code/test hashes remain frozen. I08 ran no slow test,
managed verifier, FreeCAD, GUI, daemon, `Popen`, installer or network
operation. HEAD/upstream remain
`15d58794b67c17794cdcb583b84be7a7c5a0cbfe`; the index is empty and no real
process or M00 temporary root was observed.

Fresh sol-max R08 and independent terra-medium M08 restart on the new
artifact-inclusive snapshot. R08 must prove the semantic distinction between
a normal no-publication result and every exceptional publication state, while
rechecking all prior cleanup/evidence/lifecycle findings. M08 repeats the
complete non-real suites, managed-verification stability, static/hash/Git
checks and zero-residue observations. A real GUI launch remains forbidden
until both gates pass the identical candidate.

| Entry ID | Decision / approval | Commit / push | Gate evidence | Residual | Snapshot | State |
|---|---|---|---|---|---|---|
| MRG1-G1-C01-E15 | A04 C01; sol-high I08 publication fail-closed recovery | `not-created`; GUI launch count 1 | M07 PASS on superseded I07; R07 NO-GO 0 blocker / 1 major / 0 minor / 0 nit; I08 RED 5/2 then 7; C01 91+1; status-inclusive 168+1; static gates PASS | fresh sol-max R08 and terra-medium M08 | MRG1-S22 | I08 frozen / new launch forbidden |

## 83. G1-C01 R08/M08 and I09 control-flow recovery

Independent terra-medium M08 passed the complete I08 snapshot. Each of three
managed full verifications observed an exact receipt, one verification child,
return code zero, empty stdout/stderr, verification true and an exactly equal
fresh generation recapture; all three generations were identical. I08
targeted returned `7 passed`, C01 returned `91 passed, 1 deselected`, the
status-inclusive gate returned `168 passed, 1 deselected`, and all
static/hash/Git/status/zero-residue checks passed.

Fresh sol-max R08 closed R07's publication finding and all R06 findings, but
returned NO-GO with zero blocker, one major, zero minor and zero nit.
`_cleanup_before_semantics()` caught every parse `BaseException` and converted
it only to bounded diagnostic text. A one-shot `KeyboardInterrupt` or
`SystemExit` could therefore be swallowed: the later semantic validator's
second parse succeeded and ordinary semantics ran. R08 reproduced both cases
with one semantic invocation. Its remaining publication, cleanup/evidence,
finalizer, enum/lifecycle and non-real suite attacks all passed. M08 cannot
release that control-flow gap.

The sol-high I09 correction modified only
`tests/test_freecad_workbench_gui.py`. The targeted selection first returned
`4 failed`: one case each for `KeyboardInterrupt`, `SystemExit` and
`GeneratorExit`, plus a combined failure-order case. The same selection then
returned `4 passed`.

I09 preserves the original non-`Exception` parse `BaseException` object,
finishes cleanup and one bounded evidence attempt, and then raises it. When
failures combine, the deterministic order is cleanup, action, parse-control
and evidence. Semantics does not execute and the finalizer cannot re-enter
cleanup. Ordinary parse `Exception` values remain bounded diagnostics and the
normal semantic validator remains authoritative.

Post-format non-real evidence:

```text
I09 targeted:
  RED 4 failed
  GREEN 4 passed
C01 package + controller + GUI, non-slow:
  95 passed, 1 deselected
status + C01 package + controller + GUI, non-slow:
  172 passed, 1 deselected
Ruff / format / AST / XML / diff:
  PASS
```

The only I08 code/test hash changed by I09 is:

```text
tests/test_freecad_workbench_gui.py:
  2a7ecd827da7e540d88fb90802cb11b2631de778814c6c770daabb4600afdf8b
```

All other Section 81 code/test hashes remain frozen. I09 ran no slow test,
managed verifier, FreeCAD, GUI, daemon, `Popen`, installer or network
operation. HEAD/upstream remain
`15d58794b67c17794cdcb583b84be7a7c5a0cbfe`; the index is empty and no real
process or M00 temporary root was observed.

Fresh sol-max R09 and independent terra-medium M09 restart on the new
artifact-inclusive snapshot. R09 must re-run the control-flow, publication,
strict cleanup/evidence and enum/lifecycle attack matrices. M09 repeats the
complete non-real suites, managed verification stability and all
static/hash/Git/zero-residue checks. A real GUI launch remains forbidden until
both gates pass the identical candidate and a clean cold-start preflight is
recorded.

| Entry ID | Decision / approval | Commit / push | Gate evidence | Residual | Snapshot | State |
|---|---|---|---|---|---|---|
| MRG1-G1-C01-E16 | A04 C01; sol-high I09 control-flow recovery | `not-created`; GUI launch count 1 | M08 PASS on superseded I08; R08 NO-GO 0 blocker / 1 major / 0 minor / 0 nit; I09 RED 4 then 4; C01 95+1; status-inclusive 172+1; static gates PASS | fresh sol-max R09 and terra-medium M09 | MRG1-S22 | I09 frozen / new launch forbidden |

## 84. G1-C01 R09/M09, selector recovery and second real GUI result

Fresh sol-max R09 returned GO with zero blocker, zero major, zero minor and
zero nit. Its control-flow, publication, cleanup/evidence, post-capture,
malicious-output, enum/lifecycle and harness fail-fast matrices all passed.
The targeted adversarial selection returned `21 passed`, C01 returned
`95 passed, 1 deselected`, and the status-inclusive gate returned
`172 passed, 1 deselected`. Independent terra-medium M09 passed the identical
artifact-inclusive snapshot, including three exact managed full
verifications, all static/hash/Git/status checks and zero-residue
observations.

The first admitted post-R09 pytest invocation did not enter M00. Repository
configuration supplies `-m 'not slow'`; the exact node ID did not override
that marker expression, so pytest returned exit code 5 with
`1 deselected in 0.15s`. The test body, setup, GUI `Popen` and GUI launch count
all remained zero. No automatic retry occurred.

Read-only sol-max D03 reproduced the old invocation with collect-only as zero
selected and one deselected. Adding the explicit command-line expression
`-m slow` collected exactly one test and deselected none. D03 returned GO
recovery with zero findings: command-line `-m slow` empirically overrides the
configured expression, while the exact node ID prevents selection of any
other slow test. Candidate bytes and runtime state had not changed, so R09 and
M09 remained valid. A new invocation was admitted only after another complete
cold-start preflight.

The corrected single invocation was:

```text
VIBECAD_RUN_INTEGRATION=1 \
VIBECAD_FREECAD_ENV='/Users/wangtao/Library/Application Support/VibeCAD/runtime/mamba/envs/vibecad' \
PYTHONPATH=src \
PYTHONDONTWRITEBYTECODE=1 \
.venv/bin/python -B -m pytest -q -s -p no:cacheprovider -m slow \
  tests/test_freecad_workbench_gui.py::test_real_managed_freecad_gui_workbench_m00
```

Its immediate preflight passed the nine frozen hashes, HEAD/upstream, empty
index, exact status, cold daemon/process/temp state, canonical managed
selection and receipt, one exact full managed verification, and exact GUI
binary identity. The invocation then performed exactly one GUI launch and
returned exit code 1 with `1 failed in 8.41s`; it was not retried.

The new cleanup-first bounded parent evidence succeeded:

```text
evidence count:
  1
child return code:
  1
timed out / action error / parse error:
  false / null / null
GUI status:
  error
GUI error:
  RuntimeError: expected one registered VibeCAD Workbench, observed 0
cleanup:
  clean=false
  detail=gui=exited;identity_rechecked;daemon=publication_unproven
  retire_attempted=false
  term_sent=false
  kill_sent=false
```

The GUI executable spelling and identity were exact. The child ran its single
nested unittest and exited normally after the early registration assertion.
Its stderr contained only the missing optional
`3DconnexionNavlib.framework` diagnostic. No host, Dock, gateway or daemon
code ran, no daemon publication occurred, and postflight found no related
process, M00 temporary root, endpoint or receipt. All nine hashes, Git/index
state and the exact campaign status remained unchanged.

Read-only sol-max D04 located a high-confidence module-discovery root cause.
`_gui_command()` passed `repo/freecad` to FreeCAD's `-M/--module-path`.
FreeCAD's official startup documentation defines this argument as an actual
module directory and illustrates it with a path such as
`~/.FreeCAD/Mod/Draft`, not the containing `Mod` directory. VibeCAD's actual
module directory is `repo/freecad/VibeCAD`, the directory that directly
contains `Init.py`, `InitGui.py` and `package.xml`. Passing its parent exactly
explains a normal GUI/test-runner startup with zero registered VibeCAD
workbenches. The remaining candidates rank lower: test-runner ordering,
swallowed `InitGui.py` registration failure, private user paths and the
optional Navlib warning.

I10 is a non-product, sol-high recovery limited to the GUI command, harness
and non-real GUI tests. It must first freeze RED evidence for the direct module
root, then pass `repo/freecad/VibeCAD` to `-M`. It must also validate that the
canonical module root directly contains the reviewed init and manifest files
and improve bounded registration diagnostics without changing workbench
loading behavior. No new real launch is admitted until fresh R10/M10 gates
pass the I10 artifact-inclusive snapshot.

| Entry ID | Decision / approval | Commit / push | Gate evidence | Residual | Snapshot | State |
|---|---|---|---|---|---|---|
| MRG1-G1-C01-E17 | A04 C01; autonomous technical recovery; second real GUI launch consumed | `not-created`; current recovery launch count 1 | R09 GO 0/0/0/0; M09 PASS; selector invocation body/launch 0; D03 GO; corrected invocation one GUI launch; bounded error registered Workbench count 0; zero residue; D04 direct-module-root chain | I10 direct `-M` module root and bounded registration diagnostics; fresh R10/M10 | MRG1-S22 | recovery / new launch forbidden |

## 85. G1-C01 I10 direct FreeCAD module-root recovery

The sol-high I10 correction modified only:

```text
tests/fixtures/freecad_workbench/gui_harness.py
tests/test_freecad_workbench_gui.py
```

The same targeted selection first returned `8 failed`: one exact command
failure, five missing direct-module-root validation cases and two missing
registration-diagnostic cases. After implementation it returned `8 passed`.

I10 now:

1. passes the canonical `repo/freecad/VibeCAD` directory to FreeCAD's `-M`
   option instead of its `repo/freecad` parent;
2. requires the repository and module-root spellings to be canonical and
   owner controlled, and requires the module root to directly contain
   canonical regular `Init.py`, `InitGui.py` and `package.xml` files;
3. rejects missing sources, source symlinks, repository aliases and the wrong
   parent level before a GUI launch;
4. enriches an early registration failure with canonical bounded evidence:
   total workbench count, at most eight normalized names, a truncation flag,
   expected-addon-root presence, `AdditionalModulePaths` and the type of a
   diagnostic-read error;
5. keeps diagnostic collection subordinate to the primary registration
   failure and does not change module loading or registration behavior.

Post-format non-real evidence:

```text
I10 targeted:
  RED 8 failed
  GREEN 8 passed
C01 package + controller + GUI, non-slow:
  102 passed, 1 deselected
status + C01 package + controller + GUI, non-slow:
  179 passed, 1 deselected
Ruff / format / AST / XML / diff:
  PASS
```

The I10 code/test hashes before this artifact entry were:

```text
tests/fixtures/freecad_workbench/gui_harness.py:
  02008e8e3cc9cb01b3b781cf92e6476b1c52a55770e17eb94beee2a91a579c32
tests/test_freecad_workbench_gui.py:
  c7db3d2e026bc63ee2c67f8851db12694f2a0faaf47104eb27f0cc6fc72e452b
```

All other Section 81 code/test hashes remain frozen. I10 ran no slow test,
managed verifier, FreeCAD, GUI, daemon, `Popen`, installer or network
operation. HEAD/upstream remain
`15d58794b67c17794cdcb583b84be7a7c5a0cbfe`; the index is empty and no real
process or M00 temporary root was observed.

Fresh sol-max R10 and independent terra-medium M10 restart on this
artifact-inclusive snapshot. R10 must attack module-root canonicality,
required-source identity, exact CLI ordering and bounded registration
diagnostics while rechecking every prior cleanup/lifecycle finding. M10
repeats the complete non-real suites, managed verification stability and all
static/hash/Git/zero-residue checks. A new real GUI launch remains forbidden
until both gates pass the identical candidate and a final cold-start preflight
is clean.

| Entry ID | Decision / approval | Commit / push | Gate evidence | Residual | Snapshot | State |
|---|---|---|---|---|---|---|
| MRG1-G1-C01-E18 | A04 C01; sol-high I10 direct-module-root recovery | `not-created`; current recovery launch count 1 | RED 8; targeted 8; C01 102+1; status-inclusive 179+1; static gates PASS; zero residue | fresh sol-max R10 and terra-medium M10 | MRG1-S22 | I10 frozen / new launch forbidden |

## 86. G1-C01 R10/M10 and I11 module-chain hardening

Independent terra-medium M10 passed the complete I10 snapshot. Its three
managed full verifications, `8 passed` targeted selection,
`102 passed, 1 deselected` C01 gate, `179 passed, 1 deselected`
status-inclusive gate, static/hash/Git checks and zero-residue observations
all passed.

Fresh sol-max R10 confirmed that the direct module root closes the observed
FreeCAD discovery failure, but returned NO-GO with three blockers, one major,
no minor and no nit:

1. **Blocker:** the module-root validator accepted world-writable repository,
   `freecad` and module-root directories and world-writable init/manifest
   files. FreeCAD could therefore execute mutable `InitGui.py` content after
   preflight.
2. **Blocker:** registration diagnostics fully materialized an arbitrary
   workbench iterable before truncating names, and an attacker-controlled
   exception class name could make the supposedly bounded diagnostic exceed
   2,000 characters.
3. **Blocker:** an `OSError` from the expected addon root's `is_dir()` check
   replaced the primary registration failure.
4. **Major:** diagnostic collection caught `BaseException`, converting
   `KeyboardInterrupt` and `SystemExit` into the ordinary primary
   `RuntimeError`.

R10's remaining alias, symlink, non-regular, unreadable, wrong-owner,
direct-module-root and existing lifecycle attacks passed. M10 cannot release
the four diagnostic/module-chain findings.

The sol-high I11 correction remained in the GUI-parent test and GUI harness
write domain. The targeted selection first returned
`16 failed, 1 passed`; after implementation it returned `17 passed`.

I11 now:

1. validates the repository, `freecad` ancestor and `VibeCAD` module root
   separately as canonical exact-owner directories with owner read/search
   access and no group/world write bits;
2. validates `Init.py`, `InitGui.py` and `package.xml` as canonical
   exact-owner, owner-readable regular files with no group/world write bits;
3. consumes at most 65 entries from a generic workbench iterable, retains at
   most 64 for bounded processing, reports exact built-in-dict length in
   constant time, and emits at most eight bounded normalized names;
4. bounds all diagnostic strings and exception type names before constructing
   canonical JSON, keeping the complete diagnostic at or below 2,000
   characters without truncating encoded JSON;
5. converts ordinary addon-stat, iterable and configuration-read exceptions
   into bounded subordinate diagnostic fields while preserving the primary
   registration failure;
6. catches only `Exception` for diagnostic reads, allowing
   `KeyboardInterrupt`, `SystemExit` and `GeneratorExit` to propagate as their
   original objects.

Post-format non-real evidence:

```text
I11 targeted:
  RED 16 failed, 1 passed
  GREEN 17 passed
C01 package + controller + GUI, non-slow:
  119 passed, 1 deselected
status + C01 package + controller + GUI, non-slow:
  196 passed, 1 deselected
Ruff / format / AST / XML / diff:
  PASS
```

The I11 code/test hashes before this artifact entry were:

```text
tests/fixtures/freecad_workbench/gui_harness.py:
  f4647f4b3aeec8d39e7734c3ffdf7458727bed61f1f6da07baf1862c76ddf728
tests/test_freecad_workbench_gui.py:
  2a734ec6f07b8c0dc9961c0e086dbea8e91e572aac83f1678c2239baab377f7e
```

One initial permissions-negative run left a pytest temporary directory
unremovable because the fixture had not restored directory permissions. I11
restored and removed only that exact test-owned target, moved permission
restoration into `finally`, and repeated both full suites without warnings.
Final observation found no pytest garbage, FreeCAD/daemon process or M00
temporary root.

I11 ran no slow test, managed verifier, FreeCAD, GUI, daemon, `Popen`,
installer or network operation. HEAD/upstream remain
`15d58794b67c17794cdcb583b84be7a7c5a0cbfe`; the index is empty.

Fresh sol-max R11 and independent terra-medium M11 restart on this
artifact-inclusive snapshot. R11 must repeat the module-chain permission,
bounded-iteration, bounded-JSON, subordinate diagnostic and control-flow
attacks together with all prior C01 invariants. M11 repeats the complete
non-real suites, managed verification stability and static/hash/Git/residue
checks. A new real GUI launch remains forbidden until both gates pass the same
candidate.

| Entry ID | Decision / approval | Commit / push | Gate evidence | Residual | Snapshot | State |
|---|---|---|---|---|---|---|
| MRG1-G1-C01-E19 | A04 C01; sol-high I11 module-chain hardening | `not-created`; current recovery launch count 1 | M10 PASS on superseded I10; R10 NO-GO 3 blocker / 1 major / 0 minor / 0 nit; I11 RED 16/1 then 17; C01 119+1; status-inclusive 196+1; static gates PASS; zero residue | fresh sol-max R11 and terra-medium M11 | MRG1-S22 | I11 frozen / new launch forbidden |

## 87. G1-C01 R11/M11 and I12 structured diagnostic recovery

Independent terra-medium M11 passed the complete I11 snapshot. The I11
targeted selection returned `17 passed` without warnings, C01 returned
`119 passed, 1 deselected`, the status-inclusive gate returned
`196 passed, 1 deselected`, and three managed full verifications were exact.
All static/hash/Git/status and zero unsafe-residue observations passed.

Fresh sol-max R11 closed every R10 finding but returned NO-GO with one blocker,
one major, no minor and no nit:

1. **Blocker:** the diagnostic trusted `expected_addon_root.is_dir()` to
   return a Boolean. A hostile path-like object could return NaN, an arbitrary
   object, string or integer; those values either broke canonical JSON or
   polluted the Boolean schema, replacing the primary registration failure.
2. **Major:** simultaneous ordinary failures while reading workbenches,
   `AdditionalModulePaths` and the expected addon root retained only the first
   unlabelled exception type. Later failures and their phases disappeared,
   leaving a scarce real-launch diagnostic materially ambiguous.

R11 confirmed the complete execution-chain permission policy, bounded
65-entry iterable consumption, exact-dict fast path, bounded names and
strings, control-flow propagation and all earlier C01 invariants.

The sol-high I12 correction remained in the GUI-parent test and GUI harness
write domain. Its diagnostic-focused selection first returned
`15 failed, 10 passed, 70 deselected`; after implementation the same selection
returned `25 passed, 70 deselected`.

I12 now:

1. accepts the addon-root existence result only when
   `type(value) is bool`;
2. converts NaN, arbitrary objects, strings, integers and `None` into
   `expected_addon_root_exists=null` plus a bounded root-phase `TypeError`;
3. replaces the singular error field with an exact built-in
   `diagnostic_read_errors` list;
4. records at most three exact plain mappings with exactly `phase` and `type`
   keys, no messages, closed phases and ASCII exception type names bounded to
   64 characters;
5. retains all ordinary concurrent diagnostic errors in deterministic order:
   workbenches, `AdditionalModulePaths`, expected addon root;
6. continues to propagate `KeyboardInterrupt`, `SystemExit` and
   `GeneratorExit` as their original objects from all three phases;
7. preserves the canonical complete JSON and 2,000-character upper bound.

Post-format non-real evidence:

```text
I12 diagnostic focused:
  RED 15 failed, 10 passed, 70 deselected
  GREEN 25 passed, 70 deselected
C01 package + controller + GUI, non-slow:
  132 passed, 1 deselected
status + C01 package + controller + GUI, non-slow:
  209 passed, 1 deselected
Ruff / format / AST / XML / diff:
  PASS
```

The I12 code/test hashes before this artifact entry were:

```text
tests/fixtures/freecad_workbench/gui_harness.py:
  45c298ab33d9809a54d3b63f2ce3872f5a6f5e8c44d5bd0f2479afc37a430dc7
tests/test_freecad_workbench_gui.py:
  674da1037a9926f968362458318ff818f50933de1c4ae92214b2eb42b3f94dc8
```

I12 ran no slow test, managed verifier, FreeCAD, GUI, daemon, `Popen`,
installer or network operation. HEAD/upstream remain
`15d58794b67c17794cdcb583b84be7a7c5a0cbfe`; the index is empty and no
FreeCAD/daemon/M00/unsafe pytest garbage was observed.

Fresh sol-max R12 and independent terra-medium M12 restart on this
artifact-inclusive snapshot. R12 must reproduce both R11 findings and the
entire module-chain, diagnostic, cleanup/publication/control-flow and
enum/lifecycle matrices. M12 repeats the complete non-real suites, managed
verification stability and static/hash/Git/residue checks. A new real GUI
launch remains forbidden until both gates pass the same candidate.

| Entry ID | Decision / approval | Commit / push | Gate evidence | Residual | Snapshot | State |
|---|---|---|---|---|---|---|
| MRG1-G1-C01-E20 | A04 C01; sol-high I12 structured diagnostic recovery | `not-created`; current recovery launch count 1 | M11 PASS on superseded I11; R11 NO-GO 1 blocker / 1 major / 0 minor / 0 nit; I12 RED 15/10 then 25; C01 132+1; status-inclusive 209+1; static gates PASS; zero unsafe residue | fresh sol-max R12 and terra-medium M12 | MRG1-S22 | I12 frozen / new launch forbidden |

## 88. G1-C01 R12/M12 and I13 encoded-budget recovery

Independent terra-medium M12 passed the complete I12 snapshot. Diagnostic
focused returned `25 passed, 70 deselected`, C01 returned
`132 passed, 1 deselected`, the status-inclusive gate returned
`209 passed, 1 deselected`, and three managed full verifications were exact.
All static/hash/Git/status and zero unsafe-residue checks passed.

Fresh sol-max R12 closed both R11 findings but returned NO-GO with one new
blocker and no other finding. The field-level diagnostic bounds did not imply
the promised complete 2,000-character bound after JSON escaping:
`_diagnostic_ascii()` retained printable double quotes and backslashes, so
eight maximal workbench names plus a maximal `AdditionalModulePaths` value and
three errors produced a canonical but 2,386-character diagnostic. R12
reproduced the double-quote and backslash cases independently. The I12
exact-Boolean, structured multi-error and control-flow contracts all passed.

The sol-high I13 correction remained in the GUI-parent test and GUI harness
write domain. Its targeted selection first returned `6 failed, 1 passed`:
backslash, double-quote and alternating-sensitive-character cases each broke
the complete and three-error budgets, while the normal macOS path case passed.
After implementation it returned `7 passed`.

I13 makes the smallest encoding-safe change: `_diagnostic_ascii()` maps
double quote and backslash to `?` along with non-printable/non-ASCII
characters. Other meaningful printable characters, including `/` and spaces
in normal macOS paths, remain unchanged. Each bounded diagnostic character
therefore occupies one JSON string character, so the complete canonical
payload remains within 2,000 characters without truncating encoded JSON.

Post-format non-real evidence:

```text
I13 targeted:
  RED 6 failed, 1 passed
  GREEN 7 passed
C01 package + controller + GUI, non-slow:
  139 passed, 1 deselected
status + C01 package + controller + GUI, non-slow:
  216 passed, 1 deselected
Ruff / format / AST / XML / diff:
  PASS
```

The I13 code/test hashes before this artifact entry were:

```text
tests/fixtures/freecad_workbench/gui_harness.py:
  6d172e03388af8cb4905da225cff107bb2e929048145d5d21ace3f96719f7d33
tests/test_freecad_workbench_gui.py:
  1130d1fe17f8afa93bd642921a8306dcf781e1ae198752c269d863ca7f63a31d
```

I13 ran no slow test, managed verifier, FreeCAD, GUI, daemon, `Popen`,
installer or network operation. HEAD/upstream remain
`15d58794b67c17794cdcb583b84be7a7c5a0cbfe`; the index is empty and no
FreeCAD/daemon/M00/unsafe pytest garbage was observed.

Fresh sol-max R13 and independent terra-medium M13 restart on this
artifact-inclusive snapshot. R13 must reproduce the complete aggregate
encoded-budget attack together with every prior diagnostic, module-chain and
C01 lifecycle invariant. M13 repeats all non-real suites, managed verification
stability and static/hash/Git/residue checks. A new real GUI launch remains
forbidden until both gates pass the same candidate.

| Entry ID | Decision / approval | Commit / push | Gate evidence | Residual | Snapshot | State |
|---|---|---|---|---|---|---|
| MRG1-G1-C01-E21 | A04 C01; sol-high I13 encoded-budget recovery | `not-created`; current recovery launch count 1 | M12 PASS on superseded I12; R12 NO-GO 1 blocker / 0 major / 0 minor / 0 nit; I13 RED 6/1 then 7; C01 139+1; status-inclusive 216+1; static gates PASS; zero unsafe residue | fresh sol-max R13 and terra-medium M13 | MRG1-S22 | I13 frozen / new launch forbidden |

## 89. G1-C01 R13/M13, third real result, bilingual README and I14

Fresh sol-max R13 returned GO with zero blocker, zero major, zero minor and
zero nit. Its aggregate encoded-budget, exact-Boolean, multi-error,
control-flow, bounded-iteration, permission, direct-module-root,
cleanup/publication and lifecycle matrices passed. Diagnostic focused returned
`32 passed, 70 deselected`, and the complete C01 non-slow gate returned
`139 passed, 1 deselected`. Independent terra-medium M13 passed the identical
artifact-inclusive snapshot, including `7 passed` I13 targeted,
`216 passed, 1 deselected` status-inclusive, three exact managed full
verifications, all static/hash/Git/status checks and zero unsafe residue.

The then-admitted single real M00 invocation passed registration, activation,
daemon and semantic execution but returned exit code 1 with
`1 failed in 36.28s`. It was not retried. The single bounded parent evidence
reported:

```text
GUI launch count:
  1
child return code / timed out:
  0 / false
action error / parse error:
  null / null
GUI status / error:
  ok / null
cleanup:
  clean=true
  detail=gui=exited;identity_rechecked;daemon=retired
  retire_attempted=true
  term_sent=false
  kill_sent=false
daemon id:
  daemon_995103b84db3dfdc13a7476f4878653a
```

The Workbench registered exactly once as `VibeCADWorkbench`, activated,
connected one client, refreshed through the daemon and closed the authenticated
daemon cleanly. All six lifecycle snapshots passed:

```text
initial:  inactive / dock 0 / client 0
starting: starting / dock 1 / client 0
active:   active / dock 1 / client 1
refresh:  active / dock 1 / client 1
stopping: stopping / dock 1 / client 1
final:    inactive / dock 0 / client 1
```

The only failure was a physical-object-tree disagreement:
`final_snapshot.dock_count` was zero while
`dock_count_after_shutdown` from `main_window.findChildren()` was one.
Postflight found no FreeCAD/daemon process, M00 root, endpoint, receipt or
unsafe pytest garbage; all hashes and Git/index/status remained unchanged.

Read-only sol-max D05 proved the Qt ownership/timing chain. `_finished()` had
already executed; `removeDockWidget()` removed the Dock from QMainWindow's
layout but did not remove the main window as QObject parent. `deleteLater()`
only queued DeferredDelete. The harness quit its nested event loop immediately
after observing logical inactive/dock-zero state, then queried
`findChildren()` before the outer FreeCAD event loop processed DeferredDelete.
The fake host concealed this because it equated layout removal with object
removal and treated `deleteLater()` as immediate destruction.

In parallel, the repository README was split into two complete language
files:

```text
README.md:
  English, 216 lines
  f9ab41b2debd703486c1c102a9b33df295468fe89d710b35050560d6fb6f0ee4
README.zh-CN.md:
  Simplified Chinese, 180 lines
  47db89084599f25adc69557f18b4c1f3f0c1776a1a8728a2058f0e0e42f9bc30
```

Both files contain reciprocal language links. Automated parity checks matched
all nine headings, eight code-fence lines, thirteen table lines, twelve links
and link targets, version sequences, all 28 public tools, key URI/MIME values,
host paths, cancellation/recovery semantics and roadmap literals. Both remain
conservative about G1 delivery and the latest completed real M00 result.

The sol-high I14 recovery modified only `host.py`, the fake host and controller
tests. Its realistic parent-tree/deferred-delete targeted selection first
returned `2 failed, 1 passed`: both normal shutdown and add-Dock failure left
the constructed Dock in the main-window object tree. After implementation the
directed gate returned `6 passed, 33 deselected`, controller returned
`39 passed`, and the combined status-inclusive non-slow gate returned
`220 passed, 1 deselected`.

I14 now:

1. distinguishes layout membership from QObject parent ownership in the fake
   Qt host;
2. models `removeDockWidget()` as layout-only and `deleteLater()` as scheduled,
   not immediate, destruction;
3. synchronously executes `hide`, layout removal and `setParent(None)` before
   scheduling deletion;
4. covers both normal finished shutdown and a constructed-but-never-started
   add-Dock failure;
5. attempts every detach step independently, retains honest layout/parent
   ownership flags after an exception, and does not publish dock zero while
   either ownership remains.

The I14 code/test hashes before this artifact entry were:

```text
freecad/VibeCAD/vibecad_workbench/host.py:
  7b65ae9e34ef013df66359012eca99dae12304cbc59eb60b9f6e58ec5045f95b
tests/fixtures/freecad_workbench/fake_host.py:
  7cfb0079cf64dbc0c57a2d3fead57dffc957402a60c53dcca8d13d02ca6c5966
tests/test_freecad_workbench_controller.py:
  fe570cb2ef75adc1c01955d1676bb3a68cb255c9d7db16b8600ef815516a576e
```

I14 and the README split ran no slow test, managed verifier, FreeCAD, GUI,
daemon, `Popen`, installer or network operation. HEAD/upstream remain
`15d58794b67c17794cdcb583b84be7a7c5a0cbfe`; the index is empty.

Fresh sol-max R14 and independent terra-medium M14 restart on the complete
artifact/code/test/README snapshot. R14 must adversarially attack synchronous
Dock detachment, honest residual ownership and fake/real Qt parity while
rechecking the full earlier C01 matrix. M14 repeats all non-real suites,
managed verification stability, bilingual README parity and all
static/hash/Git/residue checks. A new real GUI launch remains forbidden until
both gates pass the same candidate.

| Entry ID | Decision / approval | Commit / push | Gate evidence | Residual | Snapshot | State |
|---|---|---|---|---|---|---|
| MRG1-G1-C01-E22 | A04 C01; bilingual README; sol-high I14 Dock ownership recovery | `not-created`; current recovery launch count 1 | R13 GO 0/0/0/0; M13 PASS; real child rc0/GUI ok/daemon retired; only object-tree Dock 1; D05 deferred-delete chain; I14 RED 2/1 then directed 6; status-inclusive 220+1; README parity PASS | fresh sol-max R14 and terra-medium M14 on 11-hash snapshot | MRG1-S22 | I14 frozen / new launch forbidden |

## 90. G1-C01 R14/M14, verifier-command recovery and I15 fail-closed ownership

Independent terra-medium M14 passed every candidate-facing part of the I14
snapshot: the directed Dock selection returned `6 passed, 33 deselected`,
controller returned `39 passed`, C01 returned `143 passed, 1 deselected`, and
the status-inclusive gate returned `220 passed, 1 deselected`. Scoped Ruff,
format, AST, XML, diff, bilingual README parity, all eleven frozen hashes,
Git/index/status and zero-residue checks also passed.

M14 initially reported FAIL because its three managed-runtime stability
commands each observed an exact receipt but `verify=false`. No real GUI or
slow test was admitted. Read-only D06 proved this was a gate-command false
red, not runtime or candidate drift: the command had incorrectly set the
canonical managed prefix as `VIBECAD_FREECAD_ENV`, thereby selecting the
external-override branch. That branch correctly rejects overlap with
VibeCAD's private managed runtime tree. Repeating the exact evidence-bound
full verifier with `VIBECAD_FREECAD_ENV` absent returned `verify=true` three
consecutive times. All three runs had identical generation identities, one
child return code zero and empty stdout/stderr. M14 is therefore recovered
PASS, with the false-red command and correction retained as gate evidence.

Fresh sol-max R14 returned NO-GO with zero blocker, one major, zero minor and
zero nit. I14 closed the normal real-M00 object-tree root cause, but an
adversarial detach exception still lost the residual ownership session.
After either `removeDockWidget()` or `setParent(None)` failed, `_finished()`
reported the honest residual `dock_count=1` but published `inactive` and
cleared `_session`. A subsequent activation could then construct a second
Dock while the first still remained in the layout or QObject tree. The same
control-flow defect applied to an unstarted add-Dock failure followed by a
detach failure. R14 did not classify `_best_effort` catching
`BaseException` as a separate finding: merely narrowing that catch would
interrupt the remaining cleanup steps.

The sol-high I15 correction remained in `host.py` and its controller tests.
Its four-case directed selection first returned
`3 failed, 1 passed, 37 deselected`. The three expected failures reproduced
the unstarted and two finished residual-ownership paths; the passing case
proved that a `deleteLater()` failure alone remains a legal terminal state
once both main-window ownership dimensions are absent.

After the smallest product correction, the directed gate returned
`4 passed, 37 deselected`, controller returned `41 passed`, C01 returned
`145 passed, 1 deselected`, and the status-inclusive gate returned
`222 passed, 1 deselected`. Scoped Ruff and format, eleven-path AST,
`package.xml`, tracked/untracked diff and zero-process checks all passed.

I15 now:

1. retains `_session` and the non-terminal `stopping` lifecycle while either
   layout membership or QObject parent ownership remains;
2. records whether the worker thread has retired and forbids cleanup retry or
   replacement while it is still live;
3. makes a later activation retry only the retired session's synchronous
   Dock detach on the main thread;
4. constructs no new Dock, client or thread while cleanup continues to fail;
5. clears the old session and permits one replacement only after both
   ownership flags reach zero; and
6. preserves the ownership-focused terminal contract when only
   `deleteLater()` fails.

The I15 code/test hashes before this artifact entry were:

```text
freecad/VibeCAD/vibecad_workbench/host.py:
  4b4ef6a8738faaa636429e48f2a4594f91c3e6d6554f91900336f3ea6d5c7aa3
tests/test_freecad_workbench_controller.py:
  d2f01fbdb6361a911a7c81bbcfb77d2230d9c7251aae9cd017d13f2d9bbe4069
```

R14, M14/D06 and I15 ran no real FreeCAD GUI, slow test, installer, network
operation or product-shape change. HEAD/upstream remain
`15d58794b67c17794cdcb583b84be7a7c5a0cbfe`; the index is empty.

Fresh sol-max R15 and independent terra-medium M15 restart on the complete
artifact/code/test/README snapshot. R15 must attack residual-session
retention, live-versus-retired cleanup authority, persistent failure and
successful replacement while rechecking the prior M00 and C01 matrices. M15
must repeat all non-real suites, the corrected three-run managed verifier,
bilingual README parity and static/hash/Git/residue checks. A new real GUI
launch remains forbidden until both gates pass the same frozen candidate.

| Entry ID | Decision / approval | Commit / push | Gate evidence | Residual | Snapshot | State |
|---|---|---|---|---|---|---|
| MRG1-G1-C01-E23 | A04 C01; autonomous technical recovery; sol-high I15 | `not-created`; current recovery launch count 1 | M14 candidate gates PASS; D06 false-red command recovered by three exact managed verifies; R14 NO-GO 0/1/0/0; I15 RED 3/1 then directed 4; status-inclusive 222+1; static gates PASS | fresh sol-max R15 and terra-medium M15 on artifact-inclusive snapshot | MRG1-S22 | I15 frozen / new launch forbidden |

## 91. G1-C01 R15/M15, I16 deferred deletion and bilingual contract recovery

Independent terra-medium M15 passed the complete I15 snapshot. Its directed
selection returned `4 passed, 37 deselected`, controller returned
`41 passed`, C01 returned `145 passed, 1 deselected`, and the
status-inclusive gate returned `222 passed, 1 deselected`. Three corrected
managed full verifiers each ran with `VIBECAD_FREECAD_ENV` absent, observed
the exact receipt and identical generation, spawned one evidence-bound child
with return code zero and empty stdout/stderr, and returned true. Static,
bilingual README, hash, Git/index/status and zero-residue checks passed.

Fresh sol-max R15 returned NO-GO with zero blocker, one major, zero minor and
zero nit. I15 correctly prevented duplicate sessions, but still scheduled
`deleteLater()` while layout or parent ownership remained after a synchronous
detach exception. Real Qt may process that DeferredDelete and destroy the C++
Dock while the Python wrapper and ownership flags remain. Every later retry
would then operate on a deleted wrapper, leaving the session permanently
`stopping/dock_count=1`. The fake only marked deletion as scheduled and
therefore concealed this liveness and snapshot-consistency defect.

The sol-high I16 correction remained in `host.py` and its controller tests.
Its first directed RED round returned five expected failures: remove and
parent residual paths scheduled deletion too early, the unstarted residual
did the same, and normal plus failing deletion were attempted repeatedly by
duplicate `_finished()` calls. Its second three-case RED round proved that an
already attempted deletion still allowed duplicate callbacks to call
`hide()` twice more.

I16 now:

1. never schedules deletion while either main-window ownership flag remains;
2. marks a single deletion attempt before invoking `deleteLater()`, so even a
   deletion exception is never retried;
3. returns immediately from later detach calls after that attempt and no
   longer dereferences a possibly deleted wrapper;
4. keeps the live wrapper available while residual ownership is still
   retryable;
5. permits terminal state or replacement only after synchronous ownership
   reaches zero; and
6. lets only the current global session publish `_last_snapshot` or clear
   itself, preventing a stale duplicate finish from overwriting a replacement
   session's evidence.

The post-format I16 controller gate returned `41 passed`; C01 returned
`145 passed, 1 deselected`. Ruff, format, AST, XML and diff checks passed.
The final I16 hashes before this artifact entry were:

```text
freecad/VibeCAD/vibecad_workbench/host.py:
  4159f8eee30de798f2482ea9ecd4c9b3b72780b6c43b59e617e844f5e4cd4e0f
tests/test_freecad_workbench_controller.py:
  fb536a08836a39b6d298cb3f4b4343c1517e83463dffb694b5400d6472957c90
```

The first complete repository non-slow gate on the bilingual README candidate
returned `5264 passed, 110 deselected, 2 failed`. Both failures were stale
test contracts, not product or I16 failures:

```text
tests/test_agent_skill.py::test_release_documents_project_the_0_6_backend_truth
tests/test_mcpb_manifest.py::test_packaged_readme_describes_only_the_agent_first_surface
```

They still required Chinese claims inside the now-English `README.md`. Per
the circuit breaker the full gate was not retried on that snapshot.

A separate sol-high bilingual test-contract recovery modified only those two
tests. Its exact two-test selection first reproduced both failures, then
returned `2 passed`; both complete test files returned `22 passed`. The
contracts now validate the English and Simplified Chinese READMEs in their
own languages, require reciprocal links, and preserve the full 0.6.0,
28-tool, daemon, Task Kernel, Agent-first, import, cancellation, unsupported
surface, roadmap, unpublished and G1-not-delivered claims. `pyproject.toml`
continues to use the English `README.md` as its package metadata README.

The bilingual contract-test hashes before this artifact entry were:

```text
tests/test_agent_skill.py:
  01771a03ec4c75276fca9ccae2d3d8461c90a2823039d564a1004041d4cf63e8
tests/test_mcpb_manifest.py:
  51953e5b89324972c3604dbe0df10766b2f170d57288bab982efe253c716079e
```

R15, M15, I16 and the README contract recovery ran no real GUI, slow test,
installer, network action or product-shape change. HEAD/upstream remain
`15d58794b67c17794cdcb583b84be7a7c5a0cbfe`; the index is empty.

Fresh sol-max R16 and independent terra-medium M16 restart on the complete
thirteen-file artifact/code/test/README snapshot. R16 must reattack the real
DeferredDelete ordering, single attempt, wrapper terminal guard, stale finish
and replacement invariants together with all prior C01/M00 matrices. M16 must
repeat the complete repository non-slow gate, corrected managed verifiers,
README parity, static/hash/Git/status and residue checks. A new real GUI
launch remains forbidden until both gates pass the same frozen candidate.

| Entry ID | Decision / approval | Commit / push | Gate evidence | Residual | Snapshot | State |
|---|---|---|---|---|---|---|
| MRG1-G1-C01-E24 | A04 C01; autonomous technical recovery; sol-high I16 and bilingual contract tests | `not-created`; current recovery launch count 1 | M15 PASS; R15 NO-GO 0/1/0/0; I16 RED 5 then RED 3, controller 41, C01 145+1; repository gate exposed exactly two stale README tests; contract RED 2 then GREEN 2/full 22 | fresh sol-max R16 and terra-medium M16 on 13-hash snapshot | MRG1-S22 | I16 + bilingual tests frozen / new launch forbidden |

## 92. G1-C01 R16/M16 and successful real M00

Fresh sol-max R16 returned GO with zero blocker, zero major, zero minor and
zero nit. It reproduced the residual-ownership, DeferredDelete, wrapper
terminal-guard, stale-finish, live-versus-retired, replacement, partial-start,
thread/client and normal-M00 matrices. The bilingual README contracts also
retained their full English and Chinese product truth without weakening the
existing Chinese roadmap contract.

Independent terra-medium M16 passed the identical thirteen-file snapshot.
Its final evidence included:

```text
I16 directed:
  7 passed, 34 deselected
controller:
  41 passed
C01 non-slow:
  142 passed, 1 deselected
status-inclusive:
  219 passed, 1 deselected
bilingual README directed / full files:
  2 passed / 22 passed
complete repository non-slow:
  5266 passed, 110 deselected, 19 warnings
```

Three corrected managed full verifiers each ran without
`VIBECAD_FREECAD_ENV`, observed an exact receipt before and after, returned
true on an identical generation, and used exactly one child with return code
zero and empty stdout/stderr. Ruff, format, AST, XML, diff, thirteen hashes,
Git/index/status, bilingual structure and zero-residue checks passed.

The final cold-start preflight then passed, in the same controlled command
that conditionally admitted the real launch:

```text
candidate files:
  13 exact SHA-256 values
Git:
  HEAD = upstream = 15d58794b67c17794cdcb583b84be7a7c5a0cbfe
  index empty
  exact status 7 modified + 9 untracked, including 3 excluded paths
runtime:
  exact receipt
  full verification true
  unchanged generation f8933fc0a1f2bee6...
GUI binary:
  owner-controlled canonical managed target
cold state:
  no FreeCAD, daemon, M00 root, endpoint or receipt
```

The admitted single real M00 invocation then returned exit code zero with
`1 passed in 10.78s`. It launched the GUI exactly once. The bounded parent
evidence reported:

```text
child return code / timed out:
  0 / false
action error / parse error:
  null / null
GUI status / error:
  ok / null
cleanup:
  clean=true
  detail=gui=exited;identity_rechecked;daemon=retired
  retire_attempted=true
  term_sent=false
  kill_sent=false
```

The real FreeCAD 1.1 host loaded PySide/Qt 6.10.2, imported the reviewed
repository sources, registered exactly one `VibeCADWorkbench`, created
exactly one Dock and one daemon client, refreshed through daemon
`daemon_778dd4e1ddbb6c93c46ad92b54a6e4f4`, and completed asynchronous
shutdown. The final snapshot was `inactive/dock_count=0` with one client
construction and four heartbeats; the main-window physical Dock search was
also zero. The only stderr text was the host's pre-existing missing optional
3Dconnexion framework diagnostic.

Immediate postflight found no FreeCAD, FreeCADCmd, daemon, pytest or M00
temporary process/root. The exact thirteen launch hashes, Git status and
empty index remained unchanged.

After that proof, both READMEs were updated conservatively. They still state
that the complete G1 Workbench has not been delivered, but now record that
the local C01 bootstrap/lifecycle slice passed real M00 and distinguish it
from later preview/verdict, Accept/Reject, object/feature-selection and full
Workbench slices. Their new hashes are:

```text
README.md:
  770510bdff0a2399f8920664963adf444e67ce7cc8e6899485e8cbd6e189c8e9
README.zh-CN.md:
  7e594ca3a4a5d31aaf72d3e2bc01b0a8128d45f32a865cb9cf07f30202f5c322
```

The two directed bilingual contract tests returned `2 passed` and
`git diff --check` passed after this documentation update.

The C01 candidate is now eligible for final post-M00 review and mechanical
pre-stage gates. No additional real GUI launch is admitted or required. The
next product work remains the later approved G1 slices; this M00 proves only
the C01 bootstrap/lifecycle vertical slice.

| Entry ID | Decision / approval | Commit / push | Gate evidence | Residual | Snapshot | State |
|---|---|---|---|---|---|---|
| MRG1-G1-C01-E25 | A04 C01/M00; autonomous technical execution; R16/M16 release | `not-created`; successful final GUI launch count 1 | R16 GO 0/0/0/0; M16 PASS; repository non-slow 5266+110 deselected; preflight PASS; real M00 1 passed/child rc0/GUI ok/physical dock0/clean daemon retirement; postflight clean | complete G1 product surface remains later C02-C04 work; no more C01 real launch | MRG1-S22 | real M00 GREEN / final pre-stage gates next |

## 93. Post-M00 final gates, M18 cancellation residual and I17 recovery

Fresh sol-max R17 returned GO with zero blocker, zero major, zero minor and
zero nit on the post-M00 thirteen-file candidate. Its review found no README
overclaim, no remaining DeferredDelete or stale-finish defect, and no
candidate-integrity drift.

The first attempted final mechanical rerun, M17, completed its directed gates
but lost the terminal full-suite result through the tool channel. It is
retained as incomplete evidence and was never relabelled PASS. A historical
pytest process from the earlier I16 tool-channel return was then observed
until it exited naturally; no signal was sent. Its exact temporary root was
moved recoverably to:

```text
/Users/wangtao/.Trash/vibecad-pytest-254-orphan-20260727-2055
```

Reliable recovery M18 used one directly controlled native session and the
fixed basetemp `/private/tmp/vc-g1-m18-basetemp`. It returned exit code one:

```text
1 failed, 5265 passed, 110 deselected, 19 warnings in 147.12s
```

The only failure was
`test_concurrent_active_cancel_callers_converge_on_one_terminal_result`.
Some of the sixteen callers observed `TaskServicePortFailure(CONFLICT)`
instead of the one durable `StoredTaskRun`. All thirteen C01/README hashes,
Git status and the empty index remained unchanged, so this was treated as an
unexpected real gate red rather than retried for a favourable schedule.

Read-only sol-max D07 returned NO-GO with zero blocker, one major, zero minor
and zero nit. It proved a pre-existing product race outside the C01 code:

1. concurrent callers share the same non-blocking per-task lease;
2. one caller can durably complete cancellation while a stale-generation
   caller receives the expected service `CONFLICT`;
3. the store-only reconciliation fallback then performed only one durable
   readback;
4. that readback could itself encounter transient `LOCK_UNAVAILABLE` and
   return no result; and
5. the original `CONFLICT` therefore escaped even though the durable task was
   already cancelled.

The sol-high I17 correction modified only
`src/vibecad/application/agent.py` and
`tests/test_agent_application.py`. Before implementation, its deterministic
single regression test returned one failure for the intended reason, and the
three-test contract returned three failures. I17 replaced the one-shot
fallback with the existing monotonic, one-second
`_await_durable_cancellation()` readback, retaining the same task id,
cancellation lineage checks, the expected-generation floor, fail-closed
timeout behaviour and the original service-error mapping.

I17 post-change evidence was:

```text
three deterministic regressions:
  3 passed, 107 deselected
original sixteen-caller convergence test:
  1 passed
cancellation selection:
  17 passed, 93 deselected
complete test_agent_application.py non-slow:
  109 passed, 1 deselected
Ruff / format / AST / diff:
  PASS
```

Fresh independent sol-max R19 returned GO with
`Blocker/Major/Minor/Nit = 0/0/0/0`. It verified the one-second bound,
same-task and cancellation-lineage identity, generation floor, original-error
preservation, non-`TaskServiceError` behaviour, lease release, lock ordering,
no re-entrancy or global serialization change, and the three deterministic
tests together with the unchanged real concurrent-caller test. It also found
no product or architecture conflict between I17 and the thirteen-file
C01/README candidate.

Independent terra-medium M19 passed the frozen fifteen-file snapshot:

```text
four I17/concurrent-cancel tests:
  4 passed in 12.98s
Ruff / format / AST / diff:
  PASS
README and FreeCAD non-real-host directed:
  164 passed, 1 deselected in 14.24s
complete repository non-slow, native session 95395:
  5269 passed, 110 deselected, 19 warnings in 416.91s
postflight:
  all 15 SHA-256 values unchanged
  HEAD = upstream = 15d58794b67c17794cdcb583b84be7a7c5a0cbfe
  index empty
  no pytest, FreeCAD or VibeCAD daemon process
```

The nineteen warnings are the existing runtime/fork deprecation warnings;
there was no unexpected red. No M17, M18, D07, I17, R19 or M19 step launched
the real FreeCAD GUI, installer or network operation. The successful real M00
from section 92 remains the sole admitted final C01 launch.

After all evidence was captured, the exact M18 and M19 fixed basetemps were
moved to the system Trash rather than deleted:

```text
/Users/wangtao/.Trash/vibecad-m18-basetemp-20260727
/Users/wangtao/.Trash/vibecad-m19-basetemp-20260727
```

The selected Codex adapter profile for closeout is:

```text
approval: native-plan
delegation: spawn-send-wait
persistence: repo-artifact
process: native-session-poll
```

The permitted capability evidence categories are recorded exactly:

- `live capability declarations`: current `update_plan`, `spawn_agent`,
  `send_message`, `wait_agent`, `exec_command` and `write_stdin`
  declarations support the selected operations.
- `observable behavior`: those declared plan, delegation and original-session
  polling operations succeeded in this session.
- `environment identity`: Codex desktop on the current macOS workspace.
- `public configuration`: none observed.

The controller will close the frozen work as three independently revertible
commits: the two-file cancellation convergence fix, the twelve-file C01 and
bilingual README product slice, then this ledger closeout with both exact
commit hashes. Each commit uses an explicit staging allowlist and is pushed
before the next commit. The three excluded untracked paths remain preserved,
unread and unstaged.

### MRG1-S23 recovery snapshot

1. **Completed milestones:** R17 GO; M17 evidence-incomplete; M18 reproducible
   gate red; D07 root cause; I17 deterministic RED then focused GREEN; R19 GO
   0/0/0/0; M19 full repository PASS 5269/110; real C01 M00 remains PASS from
   section 92. The verified pre-commit anchor is branch
   `codex/agent-stage3` at
   `15d58794b67c17794cdcb583b84be7a7c5a0cbfe`, equal to upstream, with an
   empty index.
2. **Ordered next packets:** commit and push only the I17 two-file fix; commit
   and push only the twelve-file C01/README slice; append their hashes and
   commit/push the ledger; then inspect GitHub CI and confirm that branch
   pushes do not create the former empty release workflow. Any staging drift,
   push rejection, unexpected CI red or release-workflow regression stops
   closeout and preserves the exact observable state.
3. **Active decisions and authority:** MRG1-A04 admits C00-C04/M00; the user
   separately authorized MRG1-G1-C00B-A05/A06 and autonomous technical,
   recovery and gate work that does not change product function or form.
   FreeCAD remains first; a second CAD runtime receives architecture
   reservation only. No approval is reopened by I17 because it restores an
   already documented cancellation-convergence contract without changing
   product form.
4. **Execution discipline:** use the adapter profile and evidence record
   above; exact named staging only; never read, modify or stage `.workbuddy/`,
   `CAD_Theory_Course_Parametric_Learning.md` or
   `CAD_Theory_Course_Scripts_V8_True3000.md`; no duplicate full-suite or real
   GUI launch; stop on any out-of-allowlist change, unexpected gate red,
   ambiguous process state or remote rejection. The remaining product
   residual is G1 C02-C04: preview/verdict, Accept/Reject,
   object/feature-selection and the full FreeCAD Workbench experience.

| Entry ID | Decision / approval | Commit / push | Gate evidence | Residual | Snapshot | State |
|---|---|---|---|---|---|---|
| MRG1-G1-C01-E26 | A04 C01/M00; autonomous technical recovery; I17 | `not-created`; exact split and push sequence declared | R17 GO; M17 incomplete; M18 1/5265; D07 NO-GO 0/1/0/0; I17 RED then focused GREEN; R19 GO 0/0/0/0; M19 PASS 5269+110 | G1 C02-C04 remains; 19 known warnings; no more C01 real launch | MRG1-S23 | gates GREEN / exact commit closeout next |

## 94. G1-C01 commit, push and release-trigger closeout

The controller staged only the two I17 files, verified the cached allowlist
and diff, committed them as:

```text
9a008d349c2ce4f189abc61a6977d3494c7a4d3e
fix(agent): converge concurrent cancellation readback
```

That commit was pushed immediately to
`origin/codex/agent-stage3`. The controller then staged only the twelve
FreeCAD C01, bilingual README and associated test files, verified that cached
allowlist and diff, committed them as:

```text
d017254a02ae7a6120b3a97e2381071618ea4eef
feat(freecad): add G1 C01 workbench lifecycle
```

That commit was also pushed immediately. After both pushes, local HEAD and
the upstream tracking ref were exactly
`d017254a02ae7a6120b3a97e2381071618ea4eef`; the index was empty. The only
remaining tracked worktree modification was this rolling ledger. The only
remaining untracked entries were the three explicitly excluded user paths;
there was no other untracked mechanical-verification research document
eligible for this closeout.

GitHub's public Actions API reported no workflow run at either new SHA and no
`release.yml` run at either SHA. The newest historical branch release run
remained run `30199416174` at
`2cfbbc416d789491c1c532653b4e460c53dfac60`, created
`2026-07-26T11:03:41Z`, before the trigger correction. The repository's
current `.github/workflows/release.yml` trigger is restricted to:

```yaml
on:
  push:
    tags:
      - "v*"
```

Therefore ordinary `codex/agent-stage3` pushes no longer create the empty
release workflow failures that previously generated notification mail. The
GitHub connector's pull-request-run view also returned no run for either new
SHA. The local `gh` CLI had no separate authenticated session, so the
read-only connector and public API were used; no authentication state was
changed.

### MRG1-S24 closeout snapshot

1. **Completed milestones and commits:** I17 is
   `9a008d349c2ce4f189abc61a6977d3494c7a4d3e`, pushed; FreeCAD G1-C01 plus
   bilingual README is
   `d017254a02ae7a6120b3a97e2381071618ea4eef`, pushed. R19 is GO 0/0/0/0,
   M19 is PASS 5269/110, and the real C01 M00 is PASS as recorded in section
   92. This ledger is the sole final named staging target.
2. **Ordered next packets:** commit and push this ledger closeout; verify that
   exact final SHA on the remote and confirm once more that its ordinary
   branch push creates no release run. Then resume the already approved G1
   product sequence at C02 without another C01 real launch.
3. **Active decisions and authority:** MRG1-A04 and the later autonomous
   technical authorization remain active. FreeCAD stays the only end-to-end
   product target for the current G1 sequence; additional CAD runtimes retain
   architecture reservation only.
4. **Execution discipline and residuals:** retain the Codex capability
   profile from section 93; stage this file by its exact path only; preserve
   all excluded paths. Stop on a ledger push rejection or any observed
   release-workflow regression. The product residual is C02-C04:
   preview/verdict, Accept/Reject, object/feature selection and the complete
   FreeCAD Workbench experience. The test residual is nineteen known
   runtime/fork deprecation warnings.

| Entry ID | Decision / approval | Commit / push | Gate evidence | Residual | Snapshot | State |
|---|---|---|---|---|---|---|
| MRG1-G1-C01-E27 | A04; autonomous I17 recovery | `9a008d349c2ce4f189abc61a6977d3494c7a4d3e`, pushed | deterministic RED; focused 109+1; R19 GO; M19 full 5269+110 | 19 known warnings | MRG1-S24 | completed |
| MRG1-G1-C01-E28 | A04 C01/M00; bilingual README | `d017254a02ae7a6120b3a97e2381071618ea4eef`, pushed | R16/R17 GO; M16/M19 PASS; real M00 PASS; release runs absent for both pushed SHAs | G1 C02-C04 remains; no further C01 real launch | MRG1-S24 | completed |
| MRG1-G1-C01-E29 | A04 closeout ledger | `this ledger commit`, push immediately after exact G0 gate | exact-path diff check, content hash, Git/index/status and remote release-trigger verification | none beyond E27/E28 residuals | MRG1-S24 | closeout commit next |

## 95. G1-C02 safe-preview architecture and implementation freeze

The C01 ledger closeout was committed and pushed as:

```text
18e627c73e72a966cff3e37a7d95aab8639f2ee9
docs(orchestration): close G1 C01 evidence
```

Local HEAD and upstream were equal at that commit, the index was empty and
the tracked worktree was clean. GitHub's public Actions API reported no
workflow run and no release run at that exact SHA. The three excluded
untracked user paths remained preserved and unstaged.

G1-C02 resumes the exact MRG1-A04 authorization from Sections 28 and 29. Its
product outcome is to open two separate, non-authoritative FreeCAD Preview
Documents for the current project HEAD and the selected awaiting-review
draft. Users may visually compare them, but C02 does not yet add the
authoritative Accept/Reject action or Level-A selector capture reserved for
C03 and C04.

### 95.1 D20 architecture verdict

Read-only sol-max D20 returned GO with
`Blocker/Major/Minor/Nit = 0/0/0/0`. It inspected the complete C01 Workbench
implementation, the public checkout and file-grant facades, the daemon
protocol and the existing FreeCAD document seams. No shared Application,
daemon, checkout, grant or protocol change is required.

The exact C02 implementation contract is:

1. the existing Qt worker remains the sole owner of one
   `LocalAgentClient`; preview open, refresh and close are new gateway
   commands;
2. a HEAD source contains only `kind` and `project_id`; a draft source
   contains only `kind`, `task_id`, `draft_id` and `expected_generation`;
3. each source executes `open_checkout` followed immediately by
   `claim_file_grant` on that same worker and client, using only the grant
   returned by the open response and claiming it exactly once;
4. the worker emits plain mappings only; the GUI main thread validates
   source, open-key, descriptor, checkout, grant, digest and size identity
   before passing only the claim's exact `local_path` to
   `FreeCAD.openDocument`;
5. the new `preview.py` owns frozen HEAD/draft bindings that retain source,
   open-key, descriptor, checkout, claim and exact document object/Name
   identity; no artifact position, internal revision path, label or guessed
   path is an identity source;
6. any checkout with `dirty=true`, state other than open, source liveness
   other than live, or a Preview Document whose `Modified` is not exactly
   false permanently disables review for that open cycle and requires
   discard, close and reopen;
7. cleanup is document, then checkout, then client; registry identity must be
   exact before closing a named document; partial failure becomes
   recovery-required and never reuses a grant; and
8. Workbench code never calls save, save-as or publish. A user-triggered save
   can affect only the non-authoritative checkout and cannot become the basis
   for the old verdict.

The deterministic test-first matrix freezes these RED cases:

1. open and claim on different clients or sessions;
2. a second claim or grant reuse;
3. checkout, digest or size disagreement between descriptor and claim;
4. a relative, guessed or non-exact claimed path reaching `openDocument`;
5. HEAD and draft resolving to the same document object or Name;
6. a GUI/document operation on the worker thread;
7. a client RPC on the GUI thread;
8. dirty checkout review eligibility;
9. stale, revoked or recovery-required checkout eligibility;
10. `Document.Modified=True`, including a refresh that attempts to re-enable
    the same open cycle;
11. project, task, draft or generation identity drift and stale responses;
12. normal and partial-failure cleanup ordering, with claim and close calls at
    most once.

### 95.2 D21 real-host and V01 verdict

A second, separately bounded sol-max adversarial stage returned
GO-with-pre-commit-gate and
`Blocker/Major/Minor/Nit = 0/1/2/0`.

The major is a gate-boundary finding, not a C02 product-code NO-GO. The
existing `gui_harness.py` automates only C01
activate -> refresh -> deactivate. It does not create an awaiting-review
fixture, open previews or emit checkout/grant/document evidence, and that
path is intentionally absent from the approved C02 allowlist. Section 28
already defines G1-V01 as a controller-owned screenshot plus identity log, so
the exact product commit may proceed only after that one manual/controlled
V01 passes. Making V01 a repeatable `--run-test` harness requires a future
exact approval for `gui_harness.py` and its GUI tests; it is not smuggled into
C02.

The two minor risks become required tests and V01 observations:

- `openDocument(path)` may reuse a registered document, so HEAD and draft
  must have different objects and Names and each
  `getDocument(Name) is document`;
- Qt DeferredDelete is not document cleanup evidence. Both documents must be
  synchronously closed and absent from the FreeCAD registry before checkout
  or client cleanup is queued.

The single admitted G1-V01 contract is:

```text
cold preflight:
  authenticated managed prefix and exact GUI identity
  isolated owner-private VIBECAD/FreeCAD roots
  no GUI, daemon, checkout or grant residue
fixture:
  public APIs only
  one project with FCStd HEAD
  one awaiting_user_review task/draft
  exact project/task/draft/generation recorded
machine evidence:
  main/worker thread identities and daemon id
  both source/open-key/descriptor/grant/checkout records
  claimed path identity, digest and size
  both document Names, registry object identity and Modified state
  document -> checkout -> client close sequence
  final pid/socket/run-root/checkout/grant absence
screenshot:
  selected project/task in the Dock
  separate HEAD and draft document tabs
  connected/live/clean state
  no absolute path or real-user root
PASS:
  every identity agrees, documents are distinct, no modal/reuse occurs,
  screenshot is complete and cleanup is clean
FAIL:
  any collision, partial create, Modified state, disconnect, claim/close
  failure, order defect or residue; no retry-until-green
```

### 95.3 Exact C02 implementation packet and recovery snapshot

The source packet keeps the original approved subject:

```text
feat(workbench): preview managed head and draft
```

Its exact allowlist remains:

```text
M docs/orchestrated/vibecad-multi-runtime-g1.md
M freecad/VibeCAD/vibecad_workbench/state.py
M freecad/VibeCAD/vibecad_workbench/gateway.py
M freecad/VibeCAD/vibecad_workbench/dock.py
M freecad/VibeCAD/vibecad_workbench/host.py
A freecad/VibeCAD/vibecad_workbench/preview.py
M tests/fixtures/freecad_workbench/fake_host.py
M tests/test_freecad_workbench_controller.py
A tests/test_freecad_workbench_preview.py
```

The implementation subagent may modify only the eight source/test paths; the
controller alone owns this artifact. It must capture a genuine deterministic
RED before production bytes, implement the smallest contract, then run
G1-G02 and affected C01 regressions. A new or shared seam, a public
tool/protocol change, Accept/Reject, selector work, save/publish behavior,
real GUI launch before review/mechanical gates, or any excluded-path access is
a circuit breaker.

#### MRG1-S25

1. **Completed:** C01 and its ledger are pushed through `18e627c`; D20 is GO
   0/0/0/0; D21 is product-code GO with one manual V01 pre-commit gate and two
   testable real/fake parity risks.
2. **Next:** run an independent terra-medium G0 on this append and persist it;
   delegate exact RED-first implementation to sol-high; obtain sol-max
   adversarial and terra-medium mechanical gates; only then admit one V01.
   V01 PASS permits exact staging/commit/push; any V01 red stops without
   repeat or scope expansion.
3. **Authority:** MRG1-A04 remains the exact approval. The user's standing
   autonomy covers this technical decomposition because it does not change
   the approved C02 product outcome. C03/C04, automatic V01 harness
   expansion, packaging and release remain separate boundaries.
4. **Discipline:** approval=native-plan,
   delegation=spawn-send-wait, persistence=repo-artifact and
   process=native-session-poll. Use only the four evidence categories in
   section 93, exact named staging and the dynamic exclusions above.

| Entry ID | Decision / approval | Commit / push | Gate evidence | Residual | Snapshot | State |
|---|---|---|---|---|---|---|
| MRG1-G1-C02-E01 | A04 exact C02; autonomous technical decomposition | `not-created`; artifact G0/persistence next | D20 GO 0/0/0/0; D21 GO-with-gate 0/1/2/0; exact contract and 12 RED cases frozen | V01 manual gate required; automated harness expansion separately approved only | MRG1-S25 | design GREEN / source not started |

## 96. G1-C02 I22 first candidate and R22 fail-closed correction

The Section 95 design freeze passed independent terra-medium pre-stage and
cached-only G0 gates, was committed as
`0e1d2b0db9523685dacbdb5261ac87a14c1dfbf6` with subject
`docs(orchestration): freeze G1 C02 preview contract`, and was pushed. HEAD
and upstream were equal and the index was empty before source work.

Sol-high I22 captured two genuine pre-production REDs. The absent
`vibecad_workbench.preview` module produced one expected collection error.
After adding only preview, controller and fake-host tests, the clean
integration selection exited by itself with
`3 failed, 41 deselected in 0.41s`: the gateway lacked a preview event, the
Dock lacked preview actions and the host lacked preview integration. An
earlier attempt had the same three assertion failures but required Ctrl-C
because its failure path did not deactivate the fake host; that evidence was
rejected, production bytes were restored to HEAD, the test teardown was
corrected and only the self-terminating RED was accepted.

The first I22 product candidate then returned:

```text
preview tests:
  17 passed
C02 integration selection:
  3 passed, 41 deselected
G1-G02:
  61 passed
C01 non-slow regression:
  148 passed, 1 deselected
Ruff / format / AST / diff:
  PASS after one exact terra-medium six-file formatting pass
```

The first C01 regression command had exited during collection because the
checkout `src/` tree was absent from Python's import path. No test body ran.
Read-only sol-max D22 returned GO 0/0/0/0 and classified this as a
gate-command false red: the current `.venv` contains pytest and Ruff but not
an installed VibeCAD package or `.pth`, and pytest config does not add
`src`. The one admitted correction was:

```text
PYTHONPATH=src .venv/bin/python -m pytest -q -m 'not slow' \
  tests/test_freecad_workbench_package.py \
  tests/test_freecad_workbench_controller.py \
  tests/test_freecad_workbench_gui.py
```

It passed 148/1 without launching a real GUI or daemon. This checkout-only
parent pytest path does not weaken the real-GUI rule: V01 must still use the
authenticated managed binary, explicit `-P` repository paths and child
source-identity evidence.

### 96.1 R22 adversarial NO-GO

Fresh read-only sol-max R22 returned NO-GO with
`Blocker/Major/Minor/Nit = 0/4/2/0`. Normal-path evidence was green, but the
failure paths did not yet satisfy the Section 95 contract:

1. **Orphan checkout after acquisition failure.** Once `open_checkout`
   succeeds, a claim error, invalid grant/mapping or a later main-thread open
   validation failure can lose cleanup ownership. The exact checkout is then
   absent from every binding and shutdown cannot retire it.
2. **Partial or reused FreeCAD document.** After `openDocument`, a bad Name,
   Modified state, registry mismatch or duplicate identity raises without
   rolling back a document created by that call. Blind rollback would instead
   risk closing a pre-existing user document returned by path reuse.
3. **Cleanup failure retires authority too early.** One document close failure
   can discard every checkout id. One checkout close error can still be
   followed by client close and thread retirement, violating
   document -> checkout -> client and making recovery impossible.
4. **Descriptor and refresh under-validation.** Extra or malformed descriptor,
   source, source-head, generation, digest or size fields and cross-field drift
   can pass. A refresh may remain eligible after immutable identity or content
   facts change.
5. **Pending source leak.** A matching preview-open error removes the request
   but does not clear `_preview_pending_sources`, permanently disabling that
   source's button.
6. Existing tests primarily prove the successful close order and call the
   production-only `close_all` helper; they do not inject partial
   open/document/checkout close failures.

### 96.2 I22-FIX01 exact correction packet

MRG1-A04 remains sufficient because every correction is inside the approved
C02 behavior and original eight source/test paths. No public facade, protocol,
Accept/Reject, selector or product-form expansion is permitted. The
controller continues to own this artifact.

Before changing product bytes, sol-high FIX01 must add deterministic tests
that prove:

- a valid opened checkout is closed exactly once when claim or acquired
  mapping validation fails;
- a newly created/registered invalid document is closed, while a pre-existing
  reused document is never closed;
- document-close failure retains checkout/client authority and a retry can
  converge;
- checkout-close failure keeps the client and lifecycle in a recoverable
  stopping state until retry succeeds;
- descriptor, resolved source, source-head, generation, initial/current
  digest and size schemas are exact and immutable drift is sticky
  fail-closed; and
- a matching preview-open error clears the pending source and permits one
  later attempt.

The smallest correction must register cleanup ownership immediately after a
valid checkout descriptor exists, snapshot document registry identity before
open, roll back only a document proven new and still identical, retain
per-document and per-checkout cleanup state across failures, and close the
client only after every owned document and checkout is confirmed closed.

G1-M22 and V01 are forbidden on the R22-rejected bytes. FIX01 must first
return a focused RED for the new failure matrix, then focused GREEN,
G1-G02, C01 non-slow regression, static gates and a fresh sol-max review.

#### MRG1-S26

1. **Completed:** design freeze `0e1d2b0` is pushed; I22 genuine REDs and
   normal-path GREEN are preserved; D22 recovered one setup false red; M21
   static formatting is green; R22 is NO-GO 0/4/2/0.
2. **Next:** sol-high FIX01 RED then correction; fresh sol-max and
   terra-medium gates on frozen bytes; only a 0/0/0/0 review and mechanical
   PASS may admit the one G1-V01 run.
3. **Authority:** A04 exact C02 and the user's autonomous technical direction
   cover FIX01. Any shared seam, ninth source/test path, protocol or product
   expansion stops for new approval.
4. **Discipline:** keep the eight source/test paths plus this controller-owned
   artifact as the complete allowlist; preserve the three excluded paths; no
   real GUI, daemon, broad staging, retry-until-green or client retirement
   while cleanup ownership is ambiguous.

| Entry ID | Decision / approval | Commit / push | Gate evidence | Residual | Snapshot | State |
|---|---|---|---|---|---|---|
| MRG1-G1-C02-E02 | A04 C02; I22 first candidate | `not-created`; forbidden | genuine REDs; normal GREEN 17/3/61/148; D22 setup recovery; M21 static PASS; R22 NO-GO 0/4/2/0 | six exact fail-closed findings; V01 not admitted | MRG1-S26 | blocked candidate / FIX01 authorized |

## 97. G1-C02 FIX01 evidence, R23 NO-GO and FIX02 recovery packet

FIX01 first added failure-injection tests without changing the five production
paths. The first `fail_` invocation returned the expected
`12 failed, 7 passed, 56 deselected`, but a rejected production cleanup path
left its fake worker alive and pytest required Ctrl-C. That run was rejected.
A test-only, exact-session emergency cleanup seam then produced a new test
hash and the accepted self-terminating RED:

```text
12 failed, 7 passed, 56 deselected in 1.86s
```

The failures covered two acquisition orphans, two document rollback cases,
four exact descriptor drifts, one sticky refresh, one pending source leak,
one document-close retry and one checkout-close retry. The production hashes
were unchanged from the R22 candidate, and no pytest, FreeCAD, daemon or fake
worker remained.

The first FIX01 product run then returned `17 passed, 2 failed, 56 deselected`.
All seventeen product assertions were green; the two failures occurred only
because the emergency `finally` block tried to capture `host._session` after
successful production cleanup had already set it to `None`. The same hash was
not rerun. A new controller-test hash captured the exact session before
deactivation without weakening any behavior assertion. The corrected
candidate returned:

```text
focused fail_:
  19 passed, 56 deselected
preview:
  28 passed
C02/failure selection:
  22 passed, 53 deselected
C01 non-slow:
  151 passed, 1 deselected
Ruff check:
  PASS
```

Ruff format then correctly stopped the coding packet because four paths
needed purely mechanical formatting. An independent terra-medium packet
formatted only `host.py`, `preview.py`, the controller test and the preview
test. Ruff check, format check, AST parsing and exact-path diff checks passed.
On those formatted bytes, independent terra-medium gates returned:

```text
G1-G02 pytest:
  75 passed in 0.52s
G1-G02 Ruff:
  PASS
C01 non-slow:
  151 passed, 1 deselected in 0.63s
```

The frozen R23 input hashes were:

```text
state.py       4505a1aa391d982dc3840258ed5843a899f4f8d89f7e1cea41766e029e8063fc
gateway.py     9d54cc070d80887415f1994da56ede5b8b61999493b8cdced3bb575e23b81e88
dock.py        65e5af958aaad599264dc10f7114a776c902c547affe5b7dc49906c200851b14
host.py        e366c4b55c6e806fc692a7155890c9ad8862bf08eab7580bed50d6bd0172d0fa
preview.py     367fdfa8e9ce589a96bb044ee91635d9a0fcfca1af6fbe53f578949599ecd3fb
fake_host.py   47fc0b075f5a2c28b85e86a1b2062d8c044e07d06a8472ce2881eed967039c16
controller     4b4a1f6ae1ff8cf3e49cc450982f52c2ca78c19f02d1dbf6ca5178fb4f7d1913
preview test   914417f2c29f728a557f628128d672a0bd10a9ff16d2701814460488d1ea4797
```

### 97.1 R23 independent adversarial verdict

Fresh sol-max R23 returned **NO-GO** with
`Blocker/Major/Minor/Nit = 0/6/1/0`. Its deterministic fake probes showed
that normal and originally targeted failure tests were not sufficient:

1. checkout ownership still begins after full grant/descriptor validation,
   and an acquisition cleanup failure is collapsed to a generic worker error
   without an actionable checkout retry authority;
2. a clean pre-existing FreeCAD document may be accepted and later closed as
   owned, while register-then-raise and ambiguous registry deltas can be
   treated as already clean;
3. checkout-close responses are not proven to be exact closed
   acknowledgements, and a client-close exception is suppressed as success
   after client authority is discarded;
4. deactivation can close the client while a worker-side preview open remains
   in flight, after which the response can still open a document without any
   checkout cleanup;
5. the host passes a shallow copy of a recursively frozen descriptor, so
   nominal production bindings become sticky-ineligible; eligibility is also
   stored as the last individual result instead of the conjunction of all
   required bindings;
6. preview responses are checked against their original request but not the
   current project/task/draft/generation selection, and an error with a
   mismatched operation can consume a live preview request; and
7. source-head, grant and claim `schema_version=True` values pass equality
   checks as integer version 1.

R23 also confirmed that the original matching-error pending-source leak is
closed and that ordinary document-close and checkout-close exceptions retain
enough authority for the tested retry. No save, save-as, publish,
Accept/Reject or selector surface entered the candidate.

### 97.2 FIX02 exact correction packet

MRG1-A04 and the user's standing autonomous technical authorization remain
sufficient. FIX02 changes no approved product outcome and stays inside the
same eight source/test paths. The artifact remains controller-owned. A public
facade or daemon protocol change, a ninth source/test path, C03 decision UI,
C04 selection, save/publish, a real GUI launch or excluded-path access is a
circuit breaker.

Before production bytes change, sol-high must add deterministic tests that
prove:

- once a raw open result contains a valid request-bound checkout identity,
  malformed grant/descriptor/serialization and claim failures either produce
  an exactly confirmed close or retain that checkout in worker-owned
  recovery until a later exact close succeeds;
- a malformed or `state != closed` close acknowledgement never advances
  cleanup, and client-close failure never emits `closed`, retires the thread
  or drops the retained client/recovery state;
- a clean pre-existing document is rejected and never closed; accepted
  documents are exactly one new object/Name with unchanged pre-existing
  registry entries; register-then-raise, collision and multi-delta cases
  either close only a uniquely proven-new document or remain
  recovery-required without checkout/client retirement;
- every pending preview acquisition blocks client close; a response arriving
  during stopping is adopted for cleanup only and never opens a document;
- clean HEAD plus draft bindings make aggregate eligibility true on plain
  validated mappings; drift or `Document.Modified` in either binding is
  sticky false for the whole open cycle;
- response handling revalidates the current selected
  project/task/draft/generation and matches error operation to its pending
  command; stale successful opens are adopted for cleanup without document
  open; and
- each nested schema version rejects booleans and non-exact integers.

The smallest correction must keep acquisition cleanup authority on the worker
until the main thread adopts a successful checkout, keep exact pending-open
counts in the session, and make every document/checkout/client transition
acknowledgement-driven. Registry comparison must distinguish unchanged reuse,
one exact new document and ambiguous mutation. Ambiguous mutation is
recovery-required, not silently clean.

FIX02 should also expose only an internal, host-owned neutral guard/discard
seam that can verify one binding's exact registry object and
`Modified is False`, then close document before checkout. It must not expose
Accept/Reject semantics or new UI. This closes the architecture prerequisite
identified by the parallel sol-max C03 preparation and avoids a later
out-of-allowlist Host change.

The accepted FIX02 sequence is:

1. tests-only genuine, self-terminating RED on new hashes;
2. smallest production correction;
3. focused GREEN, exact G1-G02 and corrected C01 non-slow regression;
4. independent terra-medium Ruff/format/AST/diff/hash/process gate;
5. fresh sol-max adversarial review; and
6. only a `0/0/0/0` review plus mechanical PASS may admit the single
   controller-owned G1-V01.

### 97.3 Parallel V01 and C03 preparation

Read-only sol-max V01 preparation did not launch FreeCAD, create a fixture or
consume the one-shot gate. It found that the current public snapshot exposes
too little checkout/grant/document identity for the Section 95 evidence
matrix. A repository-external, controller-owned, hash-frozen transient probe
may inspect the existing private session/bindings during the one controlled
GUI launch without changing product or the committed C01 harness. The probe,
fixture and launch remain forbidden until FIX02 review and mechanical gates
are green.

Read-only sol-max C03 preparation returned architecture GO but implementation
HOLD until C02 closes. Ordinary future CAD operations remain backend
Task-Kernel/runtime semantics and should not change the FreeCAD plugin.
C03 requires the neutral Host authority/discard seam above; it must not let
the Dock close a checkout before its exact FreeCAD document or move FreeCAD
objects to the worker thread.

#### MRG1-S27

1. **Completed:** FIX01 genuine RED and behavioral/static gates are preserved;
   formatted final bytes passed 75 G1-G02 and 151 C01 regressions; R23 is
   NO-GO 0/6/1/0; V01 and C03 preparation completed read-only.
2. **Next:** sol-high FIX02 RED and correction, terra-medium mechanical gate,
   fresh sol-max review, then conditional one-shot V01. Any unexpected red,
   hang, new path, public/protocol expansion or real launch before admission
   stops the packet.
3. **Authority:** A04 exact C02 plus the user's standing autonomous
   non-product direction covers FIX02 and the neutral internal seam. V01
   remains controller-owned; C03 implementation remains held.
4. **Discipline:** approval=native-plan,
   delegation=spawn-send-wait, persistence=repo-artifact and
   process=native-session-poll. Preserve the three excluded paths, use exact
   named staging only and never treat a generic error as cleanup evidence.

| Entry ID | Decision / approval | Commit / push | Gate evidence | Residual | Snapshot | State |
|---|---|---|---|---|---|---|
| MRG1-G1-C02-E03 | A04 C02; FIX01 candidate; R23 | `not-created`; forbidden | accepted FIX01 RED 12/7/56; behavioral 19/28/22/151; final G1-G02 75; C01 151; static PASS; R23 NO-GO 0/6/1/0 | seven exact R23 findings; V01 forbidden; C03 held | MRG1-S27 | blocked candidate / FIX02 authorized |

### 97.4 FIX02 RED pre-launch process-identity breaker

Sol-high drafted eighteen `test_fix02_*` cases in only the three authorized
test/fake paths and kept all five production hashes frozen. Before the
mandated single RED invocation, the controller's process sweep observed:

```text
pid: 80948
uid/user: 501 / wangtao
start: 2026-07-28 02:53:09 local
command: /Applications/FreeCAD.app/Contents/MacOS/FreeCAD
parent: 1
```

This is not the authenticated managed binary and it has no open VibeCAD,
FCStd or isolated V01 path. The coding agent's exact command audit confirmed
that it had run only bounded reads, AST parsing and Ruff checks/formatting on
authorized test paths; it had not run pytest, imported real FreeCAD or issued
any FreeCAD/application launch command. The controller did not signal or
otherwise interact with the ambiguous process. The coding agent was stopped
and its test bytes frozen.

The FIX02 RED invocation is therefore **not consumed**. Execution may resume
only after the external process exits or the user explicitly identifies it
and authorizes an exact safe disposition. On resume, recheck the process,
production/test hashes, index, exclusions and static gates before the one
pytest invocation.

#### MRG1-S28

1. **Completed:** R23 and D24 agree that FIX02 is closable inside the existing
   eight paths; eighteen RED tests are drafted; production is unchanged.
2. **Next:** resolve PID 80948 ownership without guessing or signaling, then
   freeze hashes and run the one unconsumed FIX02 RED.
3. **Authority:** A04 covers the code/test work but does not authorize
   terminating an ambiguous user-owned system FreeCAD process.
4. **Discipline:** process identity is a circuit breaker; no pytest, real GUI,
   daemon, staging or commit while the process remains ambiguous.

### 97.5 S28 resolution and accepted FIX02 RED

The user identified PID 80948 as their own system FreeCAD launch and closed
it. The controller then proved that no FreeCAD, `freecadcmd`, pytest or
VibeCAD daemon process remained; HEAD and upstream were still
`0e1d2b0db9523685dacbdb5261ac87a14c1dfbf6`, the index was empty and all five
production hashes still matched the R23 freeze.

The final D24 sol-max architecture pass returned GO: every R23 correctness
finding can be closed inside the existing eight paths. Missing or
untrustworthy checkout identity, an ambiguous FreeCAD registry mutation and
a possibly completed `LocalAgentClient.close()` are intentionally sticky
recovery states; they must not be represented as successful cleanup. No ninth
path or public daemon/facade change is required.

Independent terra-medium formatting changed only the two FIX02 test files.
Ruff, format, AST and diff checks then passed on all three test/fake paths.
The accepted pre-RED hashes were:

```text
fake_host.py   53a6e1bf79867607bdf12091704a7575503c9ec9a83a9758dbf73b6a39949b8e
controller     0e4a8d1f32b44378e70ed0b3345b66dc43986bc484a6daca95aa6d06545526e9
preview test   d94af2b9eadf3a2954567d8e3c2e837770f1ffcc5ca5c677473a3b022902e555
```

The one unconsumed FIX02 command then ran exactly once and exited by itself:

```text
.venv/bin/python -m pytest -q \
  tests/test_freecad_workbench_preview.py \
  tests/test_freecad_workbench_controller.py \
  -k 'fix02'

36 failed, 5 passed, 75 deselected in 1.59s
```

All seven R23 findings plus the private neutral guard/discard seam were
observed. The five passes were predicted baselines for exact-new document
ownership, rollback retry, wrong-ID close refusal and strict claim version
zero/two. There was no collection/setup failure, hang or process residue, and
the command was not repeated.

#### MRG1-S29

1. **Completed:** the S28 external-process breaker is resolved without a
   signal; D24 is GO; the self-terminating FIX02 RED is accepted and frozen.
2. **Next:** sol-high implements the smallest D24 correction in the five
   production paths and may adjust only the same three tests/fake paths;
   focused GREEN precedes G1-G02 and C01 regression. Unexpected red or hang
   stops before any retry.
3. **Authority:** MRG1-A04, Section 97 and the user's autonomous technical
   direction cover the internal worker/Host state machine and neutral seam;
   no product or public-contract expansion is authorized.
4. **Discipline:** keep exact checkout ownership until exact closed
   acknowledgement, exact registry proof before checkout close, aggregate
   sticky eligibility, current-selection correlation and truthful client
   retirement. V01 remains forbidden.

| Entry ID | Decision / approval | Commit / push | Gate evidence | Residual | Snapshot | State |
|---|---|---|---|---|---|---|
| MRG1-G1-C02-E04 | A04 C02; FIX02 RED | `not-created`; forbidden | D24 GO; format/static PASS; RED 36/5/75; hashes/process/index exact | production correction and independent re-review required; V01 forbidden | MRG1-S29 | genuine RED / implementation authorized |

### 97.6 FIX02 formatted candidate and mechanical admission

Sol-high implemented D24 without changing the approved product surface or
leaving the eight-path allowlist. `state.py` and all three accepted RED
test/fake hashes remained frozen. Before formatting, the new production
hashes were:

```text
gateway.py     33d2d8f3d8719b0d76380bed2b494e5b820720855af5ca4946242c5adf4ceac8
dock.py        d37c9980dac080372488e88ed03bb2dbe9868a6a162019319438482612432de1
host.py        0f707aab52d649d526de0716b54340cc1b114481fc9b98fa6604b7571980f60b
preview.py     fbb505078c6d944b273d94d8c983220d30989d76c2210f88af0164fa920b4230
```

The frozen candidate passed each behavior command once:

```text
FIX02 focused:
  41 passed, 75 deselected in 8.51s
G1-G02 full preview/controller:
  116 passed in 8.77s
C01 affected non-slow regression:
  171 passed, 1 deselected in 9.03s
Ruff check:
  PASS
```

Ruff format-check then stopped the coding packet because only `dock.py` and
`preview.py` required mechanical formatting. No same-hash test was repeated.
Independent terra-medium formatted exactly those two paths, changed no other
hash and made no semantic or manual edit. The final formatted hashes are:

```text
state.py       4505a1aa391d982dc3840258ed5843a899f4f8d89f7e1cea41766e029e8063fc
gateway.py     33d2d8f3d8719b0d76380bed2b494e5b820720855af5ca4946242c5adf4ceac8
dock.py        ecb7ad995a8beba6bed68809e5fef67b8632d850a49f6e0fe0dad843a1a1f0d3
host.py        0f707aab52d649d526de0716b54340cc1b114481fc9b98fa6604b7571980f60b
preview.py     4de2dbfe8f4c2280c0b3964dee042fe5cf1a94442ff6ecb4b530de4eac4d7ba4
fake_host.py   53a6e1bf79867607bdf12091704a7575503c9ec9a83a9758dbf73b6a39949b8e
controller     0e4a8d1f32b44378e70ed0b3345b66dc43986bc484a6daca95aa6d06545526e9
preview test   d94af2b9eadf3a2954567d8e3c2e837770f1ffcc5ca5c677473a3b022902e555
```

On those exact formatted bytes, terra-medium ran each behavior command once
and returned:

```text
FIX02 focused:
  41 passed, 75 deselected in 8.39s
G1-G02 full preview/controller:
  116 passed in 8.76s
C01 affected non-slow regression:
  171 passed, 1 deselected in 8.97s
Ruff check / format check:
  PASS / PASS
AST parse / git diff --check:
  PASS / PASS
```

The controller independently rechecked all eight hashes, the empty index,
HEAD/upstream equality at
`0e1d2b0db9523685dacbdb5261ac87a14c1dfbf6` and the absence of FreeCAD,
pytest and VibeCAD daemon residue. Mechanical admission is therefore PASS.
A fresh sol-max adversarial review remains in progress. The candidate cannot
enter real G1-V01 until that review returns exactly
`critical/major/moderate/minor = 0/0/0/0`.

#### MRG1-S30

1. **Completed:** FIX02 production correction; genuine focused GREEN; full
   G1-G02 and affected C01 regressions; independent two-path formatting and
   new-hash mechanical PASS; controller hash/process/index verification.
2. **Next:** accept only a fresh sol-max `0/0/0/0` adversarial verdict, then
   freeze the controller-owned external probe and run the single managed
   FreeCAD G1-V01. Any finding, hash drift, foreign process or launch
   ambiguity stops before V01.
3. **Authority:** MRG1-A04 and the user's standing autonomous non-product
   direction cover the completed internal correction and gates. V01 remains
   the already approved C02 product validation; C03 implementation stays
   held until C02 closes.
4. **Discipline:** approval=native-plan,
   delegation=spawn-send-wait, persistence=repo-artifact and
   process=native-session-poll. Use only the managed private FreeCAD runtime,
   preserve all excluded paths, exact-stage only after review and V01, and
   never infer cleanup from a generic success or process disappearance.

| Entry ID | Decision / approval | Commit / push | Gate evidence | Residual | Snapshot | State |
|---|---|---|---|---|---|---|
| MRG1-G1-C02-E05 | A04 C02; FIX02 final candidate | `not-created`; forbidden | focused 41/75; full 116; C01 171/1; terra formatting/static/behavior PASS; controller hashes/process/index exact | fresh sol-max adversarial verdict required; V01 not yet consumed | MRG1-S30 | mechanical GO / review pending |

### 97.7 R26 final adversarial verdict and FIX03 packet

Fresh sol-max R26 verified all eight S30 hashes before and after its read-only
review. It performed no write, test, GUI/daemon launch, network, install,
stage, commit or push action. The result is **NO-GO**:

```text
critical / major / moderate / minor = 0 / 7 / 4 / 0
```

The seven major findings are:

1. aggregate eligibility can become true with only one binding, while a
   malformed refresh error can leave a prior true value visible;
2. unexpected worker finish and a residual detach retry can publish inactive
   and remove the session without exact document/checkout/client retirement;
3. Gateway accepts a checkout-close command without a Host-proved
   document-absence handoff;
4. inconsistent FreeCAD `listDocuments()`/`getDocument()` snapshots can make
   a reused user document look newly owned, and rollback/discard proves only
   target absence rather than exact registry-minus-target;
5. a review RPC exception can close the client and set closed while checkout
   authority remains;
6. deactivation re-entered from inside `openDocument()` can leave the newly
   opened document and pending request outside stopping cleanup; and
7. duplicate preview-open and request-ID wrap lack idempotent authority maps
   and bounded terminal tombstones, allowing orphaned checkout authority or
   unsafe ID reuse.

The four moderate findings are:

1. the unused `close_all()` callback path can mark a checkout closed when the
   callback returns no exact closed descriptor;
2. a potentially completed client close that raises is labeled
   `known_failure` instead of sticky `unknown_outcome`;
3. closed-descriptor validation incorrectly freezes mutable close-time
   fields and can blindly repeat `close_checkout` instead of querying the
   existing checkout first; and
4. the private guard uses a retained old descriptor, while observed local
   drift does not poison the open cycle permanently.

R26 reconfirmed that raw checkout identity precedes nested validation, grant
claim is one-shot, missing identity remains sticky, exact-int nested schema
checks work, consistent clean document reuse is rejected without closure,
ordinary selection correlation and operation-matched errors work, ordinary
stopping responses are cleanup-only, malformed close acknowledgements do not
advance Host cleanup and client-close exceptions suppress `closed` and thread
quit.

All eleven findings have deterministic RED seams and are closable inside the
same eight paths using private plugin state/events. There is no public
protocol, facade, ninth-path or architecture veto. FIX03 is therefore
authorized under the unchanged A04 product boundary:

1. sol-high adds exact tests/fake behavior for all eleven findings while
   keeping every production hash frozen;
2. run one self-terminating focused `fix03` RED after static/hash/process
   closure;
3. implement the smallest internal correction, then focused GREEN, full
   G1-G02 and affected C01 regression;
4. route mechanical formatting and final gates to terra-medium;
5. require a new sol-max `0/0/0/0` review before G1-V01.

Any production drift before the accepted RED, unexpected baseline,
non-terminating command, new file, public contract, product UI, real FreeCAD
launch or excluded-path access stops FIX03. G1-V01 remains unconsumed.

#### MRG1-S31

1. **Completed:** S30 mechanical PASS is preserved; R26 independently
   returned NO-GO 0/7/4/0 with eleven exact in-allowlist findings and no
   scope veto.
2. **Next:** sol-high tests-only FIX03, one genuine focused RED, smallest
   correction, terra-medium gates and fresh sol-max review. Only the final
   `0/0/0/0` result may admit V01.
3. **Authority:** MRG1-A04 plus the user's standing autonomous technical
   authorization covers FIX03 because it changes only private correctness
   and tests inside the existing C02 product contract.
4. **Discipline:** freeze all production hashes before RED; retain exact
   document-before-checkout-before-client authority; treat unexpected
   worker/client outcomes as sticky recovery; preserve the three excluded
   paths and exact-stage only after V01.

| Entry ID | Decision / approval | Commit / push | Gate evidence | Residual | Snapshot | State |
|---|---|---|---|---|---|---|
| MRG1-G1-C02-E06 | A04 C02; R26 / FIX03 | `not-created`; forbidden | final formatted mechanical PASS; R26 NO-GO 0/7/4/0; hashes exact | eleven private correctness findings; FIX03 RED/correction required; V01 forbidden | MRG1-S31 | adversarial breaker / FIX03 authorized |

### 97.8 Accepted FIX03 RED

Sol-high added eighteen deterministic `test_fix03_*` functions in only the
two existing test paths. They select as thirty nodes:

```text
eligibility cardinality and malformed refresh        3
unexpected worker finish                             1
unauthorized direct/stale checkout close             2
registry consistency and exact-minus-target          3
review exception with live checkout                  1
reentrant deactivate inside openDocument             1
request replay/conflict/wrap/tombstones               4
close_all exact acknowledgement                      1
client close-then-raise outcome                       1
closed descriptor drift and reconciliation           3
fresh guard and sticky drift                         10
```

No fake or production byte changed before RED. Ruff, format, AST and diff
checks passed on all three test/fake paths. The controller independently
verified all five production hashes, the frozen fake hash, the two new test
hashes, empty index, exact HEAD/upstream and the absence of FreeCAD, pytest
or daemon processes. The accepted test hashes are:

```text
fake_host.py   53a6e1bf79867607bdf12091704a7575503c9ec9a83a9758dbf73b6a39949b8e
controller     9088dff0073ff8fce32c3ec565fa018c1a7133f080bee29d54565b3ae4e26ad2
preview test   459ca3dbe1a2d13cd9a86e55980eaed32f2c00b70ee3b46beea888ad7ad9c4ad
```

The single admitted command then ran exactly once:

```text
.venv/bin/python -m pytest -q \
  tests/test_freecad_workbench_preview.py \
  tests/test_freecad_workbench_controller.py \
  -k 'fix03'

30 failed, 116 deselected in 6.48s
```

It exited naturally with status 1 in 6.9714 seconds. Every failure mapped to
its predicted R26 node; there was no collection/setup error, unexpected pass,
count drift, hang, hash drift or process residue. The same hash was not
rerun. FIX03 may now change only the five production paths and, only if a
genuine regression requires it, the same three test/fake paths.

#### MRG1-S32

1. **Completed:** R26 0/7/4/0 is preserved as the FIX03 basis; all eleven
   findings have a genuine, exact thirty-node RED on frozen production.
2. **Next:** sol-high implements the smallest private correction; focused
   FIX03 GREEN precedes full G1-G02 and C01 non-slow gates; terra-medium then
   owns formatting/final mechanical closure and sol-max owns the next review.
3. **Authority:** A04 and the standing autonomous technical direction cover
   the implementation because neither product shape nor public contract
   changes. A ninth path, public protocol or new user behavior remains a
   breaker.
4. **Discipline:** no same-hash RED rerun; document absence must precede
   checkout close, exact checkout close must precede normal client
   retirement, and only the expected closed transition may authorize
   inactive/session removal. V01 remains unconsumed.

| Entry ID | Decision / approval | Commit / push | Gate evidence | Residual | Snapshot | State |
|---|---|---|---|---|---|---|
| MRG1-G1-C02-E07 | A04 C02; FIX03 RED | `not-created`; forbidden | static PASS; RED 30/116; hashes/process/index exact | smallest FIX03 production correction and independent gates required; V01 forbidden | MRG1-S32 | genuine RED / implementation authorized |

### 97.9 D27 frozen-test contract reconciliation

Before changing a production byte, the FIX03 implementation packet detected
that the old `test_fix02_private_host_guard_and_single_discard_seam` required
the private guard to succeed immediately on the acquisition descriptor and
to succeed again after an observed `Modified=True` condition was manually
repaired. The new FIX03 tests require a fresh correlated refresh before each
decision guard and make every observed drift sticky for the entire open
cycle. No legitimate production state can satisfy both contracts for the
same setup and call sequence. The implementation packet stopped without an
edit or test launch and reverified all eight S32 hashes.

Sol-max D27 independently returned:

```text
current frozen tests:       NO-GO
tests-only reconciliation:  GO
collected node delta:        0
```

The coherent internal contract has two independent dimensions:

```text
cycle:                 healthy | poisoned
per-binding freshness: stale | fresh(correlated token)
```

A successful open begins stale. An exact clean `preview_refreshed` event for
that checkout creates one fresh token. The decision guard enforces GUI-thread
authority and exact local identity/cleanliness, requires an unpoisoned cycle,
then consumes that token on success. Missing freshness fails without
poisoning. Any observed local, registry or descriptor drift poisons the
entire cycle and clears freshness; repair or a later clean refresh cannot
clear poison. Poison resets only after every document and checkout from the
cycle is exactly retired and a new cycle begins.

D27 therefore retires only the unsound immediate-success, repaired-Modified
success and repaired-registry success assertions from the one old FIX02 seam
test. Privacy, wrong-thread rejection, document-before-checkout discard,
exactly one checkout close and post-discard rejection remain. The FIX03
freshness and nine drift-matrix nodes remain unchanged. This is a test
contract correction inside the existing C02/C03-neutral boundary; it changes
no product policy or public protocol and needs no new approval.

#### MRG1-S33

1. **Completed:** FIX03 RED remains genuine; the pre-implementation
   contradictory-test breaker was caught before production drift; D27
   selected one coherent safety contract with zero node-count change.
2. **Next:** make the one-function tests-only reconciliation, freeze hashes,
   preserve a genuine FIX03 RED on the reconciled bytes, then reissue the
   five-path production correction.
3. **Authority:** A04 and the standing autonomous non-product authorization
   cover retiring unsound legacy assertions. No product choice is being
   changed.
4. **Discipline:** guard freshness and aggregate eligibility are distinct;
   decision freshness is one-use and every observed drift is cycle-sticky.
   Production remains frozen and V01 remains unconsumed.

| Entry ID | Decision / approval | Commit / push | Gate evidence | Residual | Snapshot | State |
|---|---|---|---|---|---|---|
| MRG1-G1-C02-E08 | A04 C02; D27 reconciliation | `not-created`; forbidden | implementation preflight breaker; sol-max D27 GO; zero-node test amendment | reconcile one legacy test, re-freeze RED, then implement; V01 forbidden | MRG1-S33 | test-contract correction authorized |

### 97.10 Reconciled FIX03 RED

Sol-high changed only the one D27-authorized controller-test function. It
removed only the unused draft checkout local and the three unsound legacy
guard-success blocks. Ruff, format, AST and diff checks passed, and the full
controller/preview collection remains 146 nodes. The new frozen controller
test hash is:

```text
controller     3fc5f13fd0c8363697bec29fb98756270cb371c882f08a1ed9252cdba6c53895
```

The other seven source/test hashes remained exact. After independent
controller hash, index, ref and process checks, the reconciled selector ran
once:

```text
.venv/bin/python -m pytest -q \
  tests/test_freecad_workbench_preview.py \
  tests/test_freecad_workbench_controller.py \
  -k 'fix03 or fix02_private_host_guard_and_single_discard_seam'

30 failed, 1 passed, 115 deselected in 7.23s
```

It exited naturally with status 1 in 7.6021 seconds. The preserved FIX02
privacy/thread/discard seam was the sole pass and all thirty FIX03 nodes
remained RED. There was no collection/setup error, unexpected result, hang,
hash drift or process residue, and the same hash was not rerun.

#### MRG1-S34

1. **Completed:** D27 one-function reconciliation is statically clean and
   zero-node; the reconciled genuine RED is exactly 30/1/115 with frozen
   production.
2. **Next:** reissue the five-path sol-high FIX03 correction against the
   coherent freshness/poison contract, then focused/full/C01 gates,
   terra-medium mechanical closure and fresh sol-max review.
3. **Authority:** unchanged A04 and standing autonomous technical authority;
   no product or public-contract change occurred.
4. **Discipline:** tests/fake are now frozen at controller `3fc5f13f`,
   preview test `459ca3db` and fake `53a6e1bf`; a need to change them is a new
   breaker. V01 remains unconsumed.

| Entry ID | Decision / approval | Commit / push | Gate evidence | Residual | Snapshot | State |
|---|---|---|---|---|---|---|
| MRG1-G1-C02-E09 | A04 C02; D27 reconciled RED | `not-created`; forbidden | static PASS; reconciled RED 30/1/115; hashes/process/index exact | five-path FIX03 correction and independent gates required; V01 forbidden | MRG1-S34 | coherent genuine RED / implementation authorized |

### 97.11 D28 exact `close_all` acknowledgement reconciliation

The reissued FIX03 implementation preflight found a second legacy-test
contradiction before changing production. The old
`test_cleanup_is_document_then_checkout_then_client_and_is_at_most_once`
passed a checkout-close callback that returned `None` and required checkout
and client cleanup to advance. The FIX03 exact-acknowledgement test passes the
same `None` result and correctly requires recovery, retained checkout
authority and no client close. Both cannot hold for one callback contract.

R26 finding 8 already adjudicated this behavior: a checkout may be marked
closed only from a validated full closed descriptor. `None` is not closure
evidence. D28 therefore preserves the old ordering and at-most-once
assertions but changes its deterministic callback to return an exact full
closed descriptor after recording the checkout-close event. The collected
node count must remain unchanged. This is a tests-only correction of an
unsafe legacy assumption, not a product or architecture change.

#### MRG1-S35

1. **Completed:** D27 RED remains valid; the second implementation preflight
   stopped before any byte or gate; R26 already provides the D28 contract.
2. **Next:** reconcile the one old preview test, freeze a combined RED that
   includes both preserved legacy seams, then reissue FIX03 production.
3. **Authority:** A04 and standing autonomous technical authority cover the
   one-test correction; no new approval is required.
4. **Discipline:** no checkout close without a full exact descriptor; no
   client retirement while checkout authority remains; production and V01
   stay frozen.

| Entry ID | Decision / approval | Commit / push | Gate evidence | Residual | Snapshot | State |
|---|---|---|---|---|---|---|
| MRG1-G1-C02-E10 | A04 C02; R26 finding 8 / D28 | `not-created`; forbidden | second preflight breaker before writes; exact-ack contract already reviewed | reconcile one legacy callback and re-freeze combined RED | MRG1-S35 | test-contract correction authorized |

### 97.12 Final reconciled FIX03 baseline

D28 changed only the one legacy preview-test callback. It now records the
same checkout-close event and returns a deterministic exact full closed
descriptor. Event ordering, at-most-once assertions, FIX03 tests and the
146-node collection remain unchanged. Ruff, format, AST and diff checks
passed. The final frozen test hashes are:

```text
fake_host.py   53a6e1bf79867607bdf12091704a7575503c9ec9a83a9758dbf73b6a39949b8e
controller     3fc5f13fd0c8363697bec29fb98756270cb371c882f08a1ed9252cdba6c53895
preview test   41e8c5bc529ccb0d00f44e020e61faf0f71c3117283df5a3d1f96374b8a91055
```

After controller hash, index, ref and process verification, the final
reconciled selector ran exactly once:

```text
.venv/bin/python -m pytest -q \
  tests/test_freecad_workbench_preview.py \
  tests/test_freecad_workbench_controller.py \
  -k 'fix03 or fix02_private_host_guard_and_single_discard_seam or cleanup_is_document_then_checkout_then_client_and_is_at_most_once'

30 failed, 2 passed, 114 deselected in 7.08s
```

The two corrected legacy seams were the only passes. All thirty FIX03 nodes
remained genuine RED. Exit status was 1, wall time was 7.5180 seconds, and
there was no unexpected result, setup/collection error, hang, hash drift,
process residue or rerun.

#### MRG1-S36

1. **Completed:** D27 and D28 legacy contracts are coherent; the final
   combined baseline is 30/2/114 with all production bytes frozen.
2. **Next:** perform one complete static legacy-contract audit, then
   implement FIX03 in the five production paths and run the exact combined,
   full and C01 gates.
3. **Authority:** unchanged A04 and standing autonomous technical
   authorization; no product or public-contract decision is pending.
4. **Discipline:** controller/preview tests are frozen at `3fc5f13f` and
   `41e8c5bc`; any further contradiction stops before production. V01 remains
   unconsumed.

| Entry ID | Decision / approval | Commit / push | Gate evidence | Residual | Snapshot | State |
|---|---|---|---|---|---|---|
| MRG1-G1-C02-E11 | A04 C02; final reconciled FIX03 RED | `not-created`; forbidden | static PASS; combined RED 30/2/114; hashes/process/index exact | comprehensive legacy audit then five-path correction | MRG1-S36 | coherent genuine RED / implementation authorized |

### 97.13 FIX03 first candidate and I29 focused correction

The comprehensive preflight audited every affected legacy and FIX03
expectation and returned coherent with all production hashes still frozen.
Sol-high then changed only the five production paths. The first FIX03
candidate hashes were:

```text
state.py       f9a3450b1645aa757141ffd51207a342da6c7971055ff184d561f286506fb895
gateway.py     f79cac8f7a44a88b2f0ada1ec025d96e1182b78cb53cbc57d2df80822c46013f
dock.py        1c496e798c024c5f367c7974f1918cc63bf8ebe84b559f0f606ac75ddba85959
host.py        ba15d4cdf2290f7766b5db1d37ab6a8b434666df3abd82aaf7717be9ff65b96f
preview.py     fa90ad0b5063c12df50b99da5683ee8bc3bf0ab2129ed02b597c7c21b94e362b
```

All three test/fake hashes remained exact. The first combined GREEN command
ran once and stopped naturally at the required circuit breaker:

```text
2 failed, 30 passed, 114 deselected in 1.60s
```

The only failures were the local document-Modified and registry-identity
sticky-poison cases. The one-use fresh descriptor had already been consumed
by an earlier successful guard. The next guard rejected missing freshness
before inspecting the locally observable document/registry violation, so it
failed safely but did not poison the cycle; a later clean refresh could then
succeed. No same-hash rerun or later gate occurred, every hash remained
exact, and no process residue remained.

I29 is a private ordering correction on `host.py` and/or `preview.py`: inspect
and poison locally observable binding invariants before enforcing descriptor
freshness. A clean local inspection neither creates nor bypasses freshness;
the guard must still consume one exact correlated token. No test or product
contract changes.

#### MRG1-S37

1. **Completed:** full legacy audit coherent; first FIX03 candidate made
   thirty of thirty-two combined nodes green; the two remaining failures
   have one exact ordering cause and preserved frozen evidence.
2. **Next:** I29 minimal local-observation ordering correction on a new
   production hash, then combined/full/C01 gates, mechanical formatting and
   sol-max review.
3. **Authority:** A04 and standing technical authorization cover I29; no
   product or public behavior changes.
4. **Discipline:** never rerun the S37 hash; local violations poison before
   freshness rejection, while healthy local observation never authorizes a
   decision without a fresh correlated token. V01 remains unconsumed.

| Entry ID | Decision / approval | Commit / push | Gate evidence | Residual | Snapshot | State |
|---|---|---|---|---|---|---|
| MRG1-G1-C02-E12 | A04 C02; FIX03/I29 | `not-created`; forbidden | first GREEN gate 30/2/114; exact cause; hashes/process frozen | I29 new-hash correction and full gates required | MRG1-S37 | controlled implementation breaker |

### 97.14 I29 behavioral GREEN and C01 command-channel breaker

I29 changed only `host.py` and `preview.py`. It observes and poisons local
binding violations before checking the one-use correlated descriptor token;
a healthy local observation still cannot create or bypass freshness. The new
frozen hashes are:

```text
state.py       f9a3450b1645aa757141ffd51207a342da6c7971055ff184d561f286506fb895
gateway.py     f79cac8f7a44a88b2f0ada1ec025d96e1182b78cb53cbc57d2df80822c46013f
dock.py        1c496e798c024c5f367c7974f1918cc63bf8ebe84b559f0f606ac75ddba85959
host.py        c5b00c4567aa250593a01b64597d7e0600d121e7db0ea6b66b70ac809a901b6c
preview.py     eb32fa349072d33d3da101e6549f1540b035ab44a8120c08fb4f2eb9daf20e9c
```

On those bytes the combined selector and full G1-G02 pair each ran once:

```text
combined:
  32 passed, 114 deselected in 1.34s
full preview/controller:
  146 passed in 8.89s
```

The coding agent then deviated from the exact admitted C01 command. It ran
the entire `tests` directory without `PYTHONPATH=src`, exited 2 during
collection and reported 83 `ModuleNotFoundError: vibecad` errors. This is a
tool/command-channel breaker, not a behavior-gate result: none of the named
C01 surfaces ran under their required environment, so the intended C01 gate
remains unconsumed. The agent correctly stopped without a narrower retry,
post-error edit or static gate. Every hash remained exact and no process
residue remained.

The recovery packet is assigned to terra-medium. It must independently
verify hashes/processes, run only the exact named C01 command with
`PYTHONPATH=src`, then perform read-only exact-eight Ruff/format checks.

#### MRG1-S38

1. **Completed:** FIX03 combined 32 and full 146 are GREEN on the I29 hash;
   the accidental broad collection command is preserved as invalid evidence
   and did not alter the candidate.
2. **Next:** terra-medium runs the one unconsumed exact C01 gate, then static
   checks. Format RED routes to a separate exact mechanical write packet;
   behavior RED stops.
3. **Authority:** unchanged A04 and standing technical authorization; the
   recovery narrows execution back to the already approved command.
4. **Discipline:** do not count or rerun the erroneous broad invocation; do
   not let the coding agent retry; preserve exact hashes and V01 freeze.

| Entry ID | Decision / approval | Commit / push | Gate evidence | Residual | Snapshot | State |
|---|---|---|---|---|---|---|
| MRG1-G1-C02-E13 | A04 C02; I29/M30 | `not-created`; forbidden | combined 32/114; full 146; invalid broad command recorded; hashes/process exact | exact C01 and static gate pending under terra | MRG1-S38 | behavior GO / command recovery |

### 97.15 M31 final formatted FIX03 candidate

Terra-medium first ran the one unconsumed exact C01 command with
`PYTHONPATH=src`; it returned `195 passed, 1 deselected in 9.14s`. Ruff lint
passed and format-check identified only `dock.py` and `preview.py`. The
read-only packet stopped without formatting.

M31 then mechanically formatted exactly those two paths. Ruff was the sole
writer and the other six hashes remained frozen. The final candidate hashes
are:

```text
state.py       f9a3450b1645aa757141ffd51207a342da6c7971055ff184d561f286506fb895
gateway.py     f79cac8f7a44a88b2f0ada1ec025d96e1182b78cb53cbc57d2df80822c46013f
dock.py        d742d7326b5f9f423c983da0a2127915cb1f5d9034e67bd3158ca25a2a4e0325
host.py        c5b00c4567aa250593a01b64597d7e0600d121e7db0ea6b66b70ac809a901b6c
preview.py     f6a3d9c74bcf93d680ff034ed7923dfefb3ec5ada2b6e6805d997078b20486d9
fake_host.py   53a6e1bf79867607bdf12091704a7575503c9ec9a83a9758dbf73b6a39949b8e
controller     3fc5f13fd0c8363697bec29fb98756270cb371c882f08a1ed9252cdba6c53895
preview test   41e8c5bc529ccb0d00f44e020e61faf0f71c3117283df5a3d1f96374b8a91055
```

Each new-hash behavior gate ran once:

```text
combined:
  32 passed, 114 deselected in 1.34s
full G1-G02:
  146 passed in 9.07s
C01 non-slow:
  195 passed, 1 deselected in 9.11s
```

Exact-eight Ruff check, format check, AST parsing and `git diff --check` all
passed. The controller independently reverified all hashes, empty index,
HEAD/upstream equality at
`0e1d2b0db9523685dacbdb5261ac87a14c1dfbf6` and absence of FreeCAD, pytest
or daemon residue. M31 mechanical admission is PASS. A fresh sol-max review
must still return exactly `critical/major/moderate/minor = 0/0/0/0` before
the one managed real-FreeCAD V01.

#### MRG1-S39

1. **Completed:** FIX03/I29 final formatted candidate; combined 32, full 146,
   C01 195/1 and all static/mechanical gates PASS; controller state exact.
2. **Next:** fresh sol-max final-hash adversarial review. A nonzero finding
   freezes V01; only 0/0/0/0 admits the controller-owned real launch.
3. **Authority:** A04 and standing technical authorization remain sufficient;
   no product decision is pending.
4. **Discipline:** final hashes above are immutable review input; review is
   read-only; use managed private FreeCAD only after admission; exact-stage
   remains forbidden before V01 and closeout.

| Entry ID | Decision / approval | Commit / push | Gate evidence | Residual | Snapshot | State |
|---|---|---|---|---|---|---|
| MRG1-G1-C02-E14 | A04 C02; M31 final candidate | `not-created`; forbidden | combined 32/114; full 146; C01 195/1; Ruff/format/AST/diff PASS; controller exact | fresh sol-max 0/0/0/0 required; V01 unconsumed | MRG1-S39 | mechanical GO / final review pending |

### 97.16 R32 adversarial verdict and FIX04 boundary

Fresh sol-max R32 verified all eight S39 hashes before and after its read-only
review. It made no write and launched no test, GUI, daemon, network or stage
operation. The final verdict is:

```text
NO-GO
critical / major / moderate / minor = 0 / 8 / 2 / 0
```

The eight major findings are:

1. `open_checkout` effect-then-raise occurs outside the retained raw-result
   boundary and may be reported as clean `known_failure`, allowing client
   retirement while an unknown checkout exists;
2. unexpected worker finish is initially retained but reactivation cleanup
   can ignore missing retirement authorization and replace the session;
3. starting a new refresh or receiving its error does not revoke an older
   one-use freshness token or visible eligibility;
4. sticky poison is keyed to one Python binding, so discarding and reopening
   one side can wash poison while the other side of the old cycle remains;
5. a generic Dock caller can assert `document_absent=True` or issue review
   directly, bypassing Host-owned document proof and fresh dual-binding
   decision guards;
6. a malformed first command does not reserve/tombstone its recoverable
   request ID, allowing a later valid command with that ID to acquire
   side-effect authority after Dock pending state was consumed;
7. Dock pops pending state on a wrong-kind success, and Host handles
   preview-open/refresh before complete envelope correlation, permitting
   orphan authority and premature side effects; and
8. registry snapshots do not reject one object aliased under two names, so a
   new name for an existing user document can be adopted and later closed.

The two moderate findings are:

1. acquisition cleanup with a lost/malformed close acknowledgement enters a
   generic recovery phase whose global close path can call close a second
   time without first reconciling through `get_checkout`; and
2. Gateway full replay responses and Dock retired request IDs grow without
   bound in a long-lived session.

R32 reconfirmed the closures for returned raw identity, exact nested
grant/claim and boolean rejection, selection epochs, error-operation
correlation, ordinary offered-close reconciliation, sticky unknown client
close, reentrant deactivate, ordinary list/get identity, exact-minus-target
cleanup and mutable close-time fields with immutable acquisition identity.

Every safety closure remains inside the original eight paths and private
plugin control plane. An effect-then-raise open may remain sticky unknown if
public open-key idempotency is not guaranteed. Replay storage may use an
internal bounded session budget and safe rollover; it need not promise
arbitrarily old full-response replay. Neither choice changes product shape
or public daemon schema.

Before FIX04 tests or code, sol-max must consolidate these ten findings into
one implementable private-control design covering Host-only capabilities,
event correlation, refresh generations, cycle-scoped poison, request
reservation/compaction and uncertain acquisition cleanup. This avoids
another set of locally correct but cross-class-inconsistent patches.

#### MRG1-S40

1. **Completed:** S39 behavior/mechanical evidence is preserved; R32 returned
   NO-GO 0/8/2/0 with ten exact findings and no product/public-scope veto.
2. **Next:** sol-max D33 consolidated architecture, then sol-high FIX04
   tests-only RED and smallest correction, terra gates and fresh sol-max
   review. V01 remains forbidden.
3. **Authority:** A04 plus the standing autonomous technical direction covers
   D33/FIX04 because it changes only private safety, lifecycle and bounded
   bookkeeping.
4. **Discipline:** generic Dock data is never authority; Host alone proves
   document absence and decision freshness; uncertain external effects stay
   sticky; every request/event is validated before pending consumption or
   side effects; cycle poison outlives individual bindings.

| Entry ID | Decision / approval | Commit / push | Gate evidence | Residual | Snapshot | State |
|---|---|---|---|---|---|---|
| MRG1-G1-C02-E15 | A04 C02; R32/FIX04 | `not-created`; forbidden | S39 mechanical PASS; R32 NO-GO 0/8/2/0; hashes exact | D33 architecture and ten-finding FIX04 required; V01 forbidden | MRG1-S40 | adversarial breaker / architecture required |

### 97.17 D33 consolidated private control-plane design

Sol-max D33 returned **design GO / current bytes NO-GO**. One coherent state
machine closes all ten R32 findings inside the same eight paths. Safety
requires no daemon/store/public-schema/UI change. Unknown acquisition may
remain sticky rather than requiring lookup-by-open-key, and bounded replay
may guarantee permanent at-most-once without retaining arbitrary old full
responses.

The authority flow is:

```text
Dock semantic intent
  -> Host request ledger and PreviewCoordinator
  -> capability-wrapped private queued command
  -> GatewayWorker / LocalAgentClient
  -> capability-wrapped worker event
  -> Host validates and correlates without consuming
  -> Host commits transition, retires pending and projects to Dock
```

Dock no longer creates wire request IDs, sends checkout-close/review wire
commands or treats `document_absent=True` as authority. A per-session
in-process capability object is shared only by Host and Gateway and checked
by exact object identity inside private command/event wrappers. It protects
the queued internal control plane from plain-dict injection and accidental
signal use; it is not a cryptographic boundary against hostile Python
reflection in the same process.

Host owns monotonic request IDs and an exact pending ledger. Worker events
are handled in this order:

1. validate exact wrapper type, capability and lane;
2. validate the full top-level event envelope;
3. non-consumingly peek exact pending request, expected event/error operation
   and context;
4. validate nested payload and transfer any raw authority;
5. apply Host/Coordinator transition;
6. atomically retire pending; and
7. project the already-correlated result to Dock.

Wrong-kind or malformed success has zero side effect and does not consume
pending. Gateway reserves an exact recoverable request ID before full command
validation. An invalid-first ID is terminal. A monotonic high-watermark plus
a bounded replay window prevents every evicted/old ID from ever regaining
side-effect authority. Host never wraps IDs; normal intent stops before a
reserved cleanup range, and exhausting cleanup authority is sticky.

Each preview cycle has one cycle ID, records for all adopted checkout
authority, exact head/draft bindings, a coordinator-wide poison latch and a
draining/recovery phase. Partial discard and reopening one side remain in the
same cycle. Poison resets only after every cycle document and checkout is
exactly retired, no request/token remains and Gateway has no unresolved
acquisition authority.

Refresh dispatch increments a per-checkout generation, deletes the prior
token and synchronously makes decision eligibility false before enqueue. A
success mints one token only when generation, cycle, binding, selection and
exact local/remote evidence all still match. Error never restores an older
token. The private review path atomically validates one head and one draft
token from the same current cycle/selection, performs a final local
observation, consumes both tokens and only then emits an authenticated review
command. Missing tokens deny without poison; observed drift poisons.

An `open_checkout` effect-then-raise before trustworthy identity produces
sticky `open_uncertain` and `unknown_outcome`; it forbids new acquisition,
client close and retirement. Every possibly completed checkout close enters
`close_uncertain` and must call `get_checkout` before another close. An exact
closed descriptor retires; an exact original open descriptor moves to a
separate retry-ready transition; malformed/drifted evidence remains sticky.

Registry snapshots additionally require each document's exact `Name`, exact
`getDocument(name)` identity and one-to-one object identity across names. A
new name aliasing any pre-open object is ambiguous recovery and is never
closed automatically.

Unexpected worker finish before exact authorized client retirement moves the
session to retained fault. Reactivation returns that same disabled session
and never creates a replacement client/Dock. Only a capability-wrapped,
correlated exact `closed` event after complete Coordinator cleanup authorizes
thread retirement, detach and inactive state.

D33 identified legacy contract updates that must precede production:

- immediate open-descriptor aggregate eligibility becomes false until both
  current refresh tokens exist;
- acquisition cleanup lost-ack expectations become one close then reconcile,
  not two direct closes;
- direct normal-lane review/preview-close/client-close tests must use an
  authenticated Host lane or assert rejection;
- raw-dict refresh helpers must emit authenticated worker events;
- ID wrap becomes drain plus reserved cleanup, never reuse;
- single-binding guards become atomic dual-binding review guards; and
- unexpected-finish tests must reactivate and prove no replacement session.

FIX04 RED must cover lost open acknowledgement, acquisition-close reconcile,
invalid-first and evicted replay, wrong-kind/malformed success, plain
privilege spoof, authenticated Host close/review, refresh supersession/error/
old response, dual-token review and enqueue failure, partial poison
replacement/full reset, registry alias/collateral cleanup, unexpected/normal
retirement and cleanup-ID reserve.

#### MRG1-S41

1. **Completed:** R32 findings are consolidated into one in-eight-path D33
   architecture with explicit authority, correlation, bounded memory,
   freshness, cycle and lifecycle transitions.
2. **Next:** incorporate the independent control-plane critique, then
   sol-high performs tests-only legacy reconciliation plus FIX04 RED. No
   production byte changes before an exact self-terminating RED.
3. **Authority:** A04 and standing technical authorization cover D33/FIX04.
   Public lookup-by-open-key, new wire schema, indefinite replay or product
   review UI remain vetoes requiring new authority.
4. **Discipline:** Host is the sole authority issuer; every external
   uncertainty is sticky until exact evidence; IDs never wrap or resurrect;
   no side effect precedes authenticated correlation. V01 remains forbidden.

| Entry ID | Decision / approval | Commit / push | Gate evidence | Residual | Snapshot | State |
|---|---|---|---|---|---|---|
| MRG1-G1-C02-E16 | A04 C02; D33/FIX04 | `not-created`; forbidden | D33 design GO; R32 current bytes NO-GO; ten-finding RED matrix | independent critique then tests-only reconciliation/RED | MRG1-S41 | architecture GO / RED pending |

### 97.18 D33 independent control-plane conditions

An independent sol-max review also returned GO with no ninth-path, public
protocol or product/UI veto. It confirmed the single Host-to-Worker queued
signal, exact in-process capability identity, non-consuming event
correlation, high-watermark/bounded replay and retained unexpected-finish
design.

Two independent conditions strengthen the main D33 design:

1. final client `close`, not only preview-close and review, must be a
   Host-private authenticated lane; otherwise a generic Dock route can still
   attempt worker retirement when no checkout is visible; and
2. freshness needs a shared current refresh-cycle barrier in addition to
   per-checkout generation/token identity, so head and draft tokens from
   different cycles can never combine into decision eligibility.

The private capability protects against plain command data, direct Dock
routing and accidental queued-signal injection. It does not claim
cryptographic isolation from a hostile same-process Python plugin capable of
arbitrary reflection. Real PySide preservation of `Signal(object)` identity
and the FIFO behavior of the single Session signal must be observed in V01.
These are validation obligations, not architecture vetoes.

The tests-only packet must also reconcile direct legacy Gateway close/review
tests to either use a private authenticated helper or assert public-lane
rejection; replace wrap expectations with exhaustion/draining; and reserve
real refresh generation/token context rather than injecting raw dict
responses. Production remains frozen until these contracts and the complete
D33/R32 RED matrix are statically closed.

#### MRG1-S42

1. **Completed:** primary and independent D33 reviews agree on an in-boundary
   design; private client close and shared refresh-cycle barrier are binding.
2. **Next:** sol-high changes only test/fake paths for legacy reconciliation
   and deterministic FIX04 RED, then controller freezes and admits one
   focused RED.
3. **Authority:** unchanged A04/standing technical direction; capability
   routing and bounded at-most-once bookkeeping are private safety behavior.
4. **Discipline:** no raw dict is worker authority or a correlated success;
   no public lane may close checkout/client or issue review; no mixed-cycle
   tokens; V01 remains unconsumed.

| Entry ID | Decision / approval | Commit / push | Gate evidence | Residual | Snapshot | State |
|---|---|---|---|---|---|---|
| MRG1-G1-C02-E17 | A04 C02; D33 independent conditions | `not-created`; forbidden | independent sol-max GO; private close + cycle barrier required | tests-only reconciliation and FIX04 RED | MRG1-S42 | architecture frozen / RED authorized |

### 97.19 Accepted FIX04 RED

Sol-high reconciled every D33-identified unsafe legacy expectation without
deleting its safety intent, then added thirty-three `test_fix04_*` functions
expanding to forty-seven nodes. They cover:

```text
private authenticated transport and event routing      18
sticky open/close uncertainty and identity authority    7
request-ID / replay ledger                              5
refresh / review authorization                         10
unexpected worker-finish lifecycle                      1
public restricted-lane authority                        2
preview registry / cycle poisoning                      4
```

Tests-only drafting was interrupted once after prolonged silence. Controller
inspection proved that both changed test files ended at complete AST
boundaries, no process was active and production/fake were unchanged. The
remaining work was then completed in two bounded phases with explicit
checkpoints.

Pre-RED review found and corrected two test-design breakers before pytest:
the old stale-Dock node was accidentally passing only because it sent
`document_absent=False`, and five missing-capability failures could strand
fake QThreads before cleanup. The corrected attack sends an otherwise exact
public restricted close with `document_absent=True`; every host-backed failure
is inside an unconditional cleanup boundary. A final enqueue-failure
monkeypatch was also scoped and restored before cleanup to remove a secondary
diagnostic exception.

Terra-medium formatting/static closure returned exact Ruff, format, AST,
compile and diff PASS with 33 functions / 47 nodes. The frozen test hashes
are:

```text
fake_host.py   53a6e1bf79867607bdf12091704a7575503c9ec9a83a9758dbf73b6a39949b8e
controller     0227f8a3512c3e6dbfe87ff7382b31d01eabcb22257bdf38aef22ff2bbf3ea2e
preview test   3d9811ab7398a08d4038f35f00d6c221fde33bc8eb6bf847586d5da2cbfd128e
```

The controller independently verified those hashes, all five S39 production
hashes, empty index, exact HEAD/upstream and no relevant process. The final
command then ran once:

```text
.venv/bin/python -m pytest -q \
  tests/test_freecad_workbench_preview.py \
  tests/test_freecad_workbench_controller.py \
  -k 'fix04'

47 failed, 128 deselected in 19.67s
```

It exited naturally with status 1 in 20.10 seconds. There were zero passes,
collection/setup errors, hangs, secondary cleanup exceptions, hash changes
or process residues. The same hash was not rerun.

#### MRG1-S43

1. **Completed:** D33 primary/independent designs are frozen; all R32 classes
   have deterministic tests; clean FIX04 RED is exactly 47/128.
2. **Next:** sol-high implements the five-path D33 state machine in serialized
   checkpoints, then one focused FIX04 GREEN, full G1-G02, C01 regression,
   terra mechanical closure and fresh sol-max review.
3. **Authority:** A04 and standing autonomous technical authority cover the
   private implementation. Any new wire schema, ninth path, product UI,
   indefinite replay promise or daemon/store change remains a breaker.
4. **Discipline:** tests/fake are frozen at the three hashes above; Host alone
   owns wire authority; correlation precedes side effects; IDs never
   resurrect; uncertainty stays sticky; V01 remains unconsumed.

| Entry ID | Decision / approval | Commit / push | Gate evidence | Residual | Snapshot | State |
|---|---|---|---|---|---|---|
| MRG1-G1-C02-E18 | A04 C02; FIX04 RED | `not-created`; forbidden | static PASS; RED 47/128; hashes/process/index exact | five-path D33 implementation and independent gates | MRG1-S43 | genuine RED / implementation authorized |

### 97.20 C1 authenticated transport component closure

The first implementation checkpoint changed only Gateway and Dock. It
introduced the private in-process capability wrapper, the single hosted
transport entry and bounded request bookkeeping without exposing a new wire
kind or public lookup path.

An initial adversarial component review found seven fail-closed defects:
shallow mutable wrapper payloads, a non-exact projected cursor check,
refresh invalidation after dispatch, a hosted discard path capable of
consuming pending authority, incomplete enqueue cancellation, a hosted raw
emit path and an unbounded active-checkout authority set.

Seven regression functions expanding to eight nodes reproduced those
conditions as a clean targeted RED. The same sol-high writer then corrected
the component:

1. wrapper envelopes and nested mappings are immutable;
2. reservation must equal the exact projected cursor;
3. refresh invalidation precedes every hosted refresh dispatch;
4. hosted public discard is non-consuming;
5. enqueue failure uses conditional cancellation and retirement;
6. hosted mode has no raw request emit;
7. active checkout authority is bounded to eight.

The eight-node focused packet passed. A fresh independent sol-max reread
returned exact `0/0/0/0` with no critical, major, moderate or minor finding.
The frozen component hashes were:

```text
gateway.py  09badb074217bede6717cd3dc9b4992d911d6c996409f2c17ace97711d32e7ce
dock.py     33df21d384b813e288974e9ae335afafd4bc41f5c63dbb2778c6bb71e9b9f144
```

#### MRG1-S44

1. **Completed:** the C1 Gateway/Dock authority component is independently
   closed at `0/0/0/0`; hosted projection is capability-bound and bounded.
2. **Next:** integrate the Host ledger/correlation and Preview lifecycle
   state machine without reopening C1 or adding a public protocol path.
3. **Authority:** A04 and standing technical authorization cover the private
   in-process transport; product/UI and public protocol remain unchanged.
4. **Discipline:** Gateway/Dock hashes are frozen for Host integration; a
   later hash change requires a new focused gate and independent reread.

| Entry ID | Decision / approval | Commit / push | Gate evidence | Residual | Snapshot | State |
|---|---|---|---|---|---|---|
| MRG1-G1-C02-E19 | A04 C02; C1 component | `not-created`; forbidden | focused 8 PASS; sol-max `0/0/0/0` | Host/Preview integration | MRG1-S44 | component GO / integration pending |

### 97.21 C2 Host and Preview integration candidate

The serialized C2 implementation completed the frozen D33 state machine.
Host now owns the sole capability, queued transport and request ledger; it
validates wrapper identity, envelope shape and exact request correlation
before any projection or side effect. It also owns monotonic normal and
cleanup request IDs, bounded replay/high-water retirement, shared refresh
generation barriers, one-use head/draft review tokens, client checkout
cleanup and exact closed-retirement authorization.

Preview now keeps name/object registry identity one-to-one, applies
coordinator-wide cycle poison, treats open/close uncertainty as sticky and
fail-closed, requires complete exact retirement before a new clean cycle and
retains the same session after an unexpected worker finish. There is still
one GUI thread, one worker, one client and one Host-to-Worker
`Signal(object)` path.

The resulting implementation hashes before integration gates were:

```text
state.py    f9a3450b1645aa757141ffd51207a342da6c7971055ff184d561f286506fb895
gateway.py  09badb074217bede6717cd3dc9b4992d911d6c996409f2c17ace97711d32e7ce
dock.py     33df21d384b813e288974e9ae335afafd4bc41f5c63dbb2778c6bb71e9b9f144
host.py     c14b1e622c5bc8c5f19e6d23522b7d1e11ab14a55be48ec8057e828c4371310a
preview.py  dd94f63ac17b08edf24d936ce48b836c011e55e7fc55a84fe75507f51c5a36b2
```

Controller inspection independently confirmed the exact hashes, empty
index, unchanged HEAD/upstream and no FreeCAD, pytest or daemon residue
before admitting one focused FIX04 gate.

#### MRG1-S45

1. **Completed:** all D33 production paths are integrated inside the original
   eight-path boundary; no ninth path, daemon/store or public wire change was
   introduced.
2. **Next:** run the self-terminating focused FIX04 gate once; any failure
   freezes the candidate and returns to diagnosis rather than same-hash retry.
3. **Authority:** unchanged A04/standing technical authority.
4. **Discipline:** V01 remains forbidden until focused, full component, C01,
   static and independent adversarial gates all close.

| Entry ID | Decision / approval | Commit / push | Gate evidence | Residual | Snapshot | State |
|---|---|---|---|---|---|---|
| MRG1-G1-C02-E20 | A04 C02; C2 integration | `not-created`; forbidden | exact hashes/index/HEAD/process precheck PASS | FIX04 focused gate | MRG1-S45 | candidate frozen / gate admitted |

### 97.22 Focused breaker and I40 recovery

Terra-medium ran the exact focused command once on the frozen C2 candidate:

```text
.venv/bin/python -m pytest -q \
  tests/test_freecad_workbench_preview.py \
  tests/test_freecad_workbench_controller.py \
  -k 'fix04'

8 failed, 47 passed, 128 deselected in 16.10s
```

The breaker stopped all later full, C01, Ruff, GUI and runtime gates. No
same-hash retry occurred.

Parallel sol-high diagnosis and independent sol-max adversarial review
converged on three localized causes:

1. six review-token failures share one Host freshness defect:
   `_selection_stamp()` includes Dock's `_task_load_epoch`, although the same
   valid refresh necessarily advances that pagination epoch; remove only
   that operational epoch while retaining project/task/draft/generation and
   both selection epochs;
2. hosted public restricted methods correctly cannot gain authority, but
   their exception conflicts with the existing non-throwing UI callback
   contract; return `None` before every status, sequence, pending or transport
   mutation while preserving the private `_send()` hard rejection;
3. the Preview test-local Host uses `len(documents) + 1`, so a partial close
   reuses and overwrites a still-live document name; replace it with a
   monotonic non-reusing allocator and do not weaken production registry
   identity checks.

The frozen candidate remains NO-GO, but the review found zero critical or
major vulnerability and no restricted-authority bypass. I41 is authorized as
an in-place local correction to Host, Dock and the defective test fixture;
Gateway, Preview, state, fake host, public protocol and product behavior stay
frozen.

#### MRG1-S46

1. **Completed:** the first C2 focused gate stopped at exact `8/47/128`; all
   failures are explained by two fail-closed production defects and one
   test-fixture identity collision.
2. **Next:** sol-high applies I41 as one writer; controller freezes new
   hashes, terra-medium performs static closure, then one new-hash focused
   gate is admitted.
3. **Authority:** standing technical authorization covers compatibility,
   freshness and test-fixture correction; no product approval is required.
4. **Discipline:** remove only task-load epoch; public restricted no-op must
   be zero-state; Preview registry/cycle checks must not be relaxed.

| Entry ID | Decision / approval | Commit / push | Gate evidence | Residual | Snapshot | State |
|---|---|---|---|---|---|---|
| MRG1-G1-C02-E21 | A04 C02; I40/I41 recovery | `not-created`; forbidden | focused breaker 8/47/128; sol-high + sol-max cause agreement | localized new-hash correction and rerun | MRG1-S46 | NO-GO / recovery active |

### 97.23 I41 correction, static breaker and final formatting

Sol-high applied the I40 correction as one writer:

1. Host's selection stamp changed from seven fields to six by removing only
   Dock's task-load epoch;
2. all four hosted public restricted methods return `None` before status,
   sequence, pending, transport or authority state can change, while
   `_send()` retains its internal hard rejection;
3. the Preview test-local Host now allocates monotonically unique document
   names; and
4. the legacy hosted-close regression now covers preview-close, review,
   client-close and aggregate close with exact zero-state assertions.

Controller independently confirmed the new hashes, empty index, unchanged
HEAD/upstream and no relevant process. It also found and precisely retired
one sleeping pytest process left by the earlier focused gate: PID 5476,
process group 5476, parent Codex app-server, exact repository cwd and exact
FIX04 command. A single exact `SIGTERM` retired it; no broad process match or
user process was signalled.

The first I42 static gate stopped before pytest on three Ruff findings: one
Host import-order issue and two unused assignments. I43 preserved the
existence-validating Host call, removed only its unused assignment, removed
the genuinely unused Preview binding key and organized imports. Ruff
formatting then revealed that Gateway and Dock also needed purely mechanical
formatting. Terra-medium formatted those exact two paths and reran all
static checks.

The final formatted eight-path hashes are:

```text
state.py       f9a3450b1645aa757141ffd51207a342da6c7971055ff184d561f286506fb895
gateway.py     070c4b73f82dc313e1570bb8a338cc5c8b173ca5cf4253bc7502f38f4edba27c
dock.py        13845e833cc359e616f011db9538681ea7a6d094479206b2927848fc0d9921af
host.py        0708b77828c37ad8037d1aef8adb4679e0b9bf5ddf820150ef1d04fa123846c8
preview.py     c44b7f2da892a7551628826f6bf2b5fd72f03d02c58fc2e6c812a06172560635
fake_host.py   53a6e1bf79867607bdf12091704a7575503c9ec9a83a9758dbf73b6a39949b8e
controller     f38df4184abcbbbf04a7c8772104d9413b4a18ee5c2e21d4dfa4f5e7b66e928b
preview test   03cb23c31df0346c0948b820932939ef022c05d4f01c1df131fb09b43a1ad5ca
```

On those exact bytes Ruff check, Ruff format-check, AST/in-memory compile and
`git diff --check` all passed.

#### MRG1-S47

1. **Completed:** I41 is integrated and all eight paths are in one
   reproducibly formatted, lint-clean, syntax-clean candidate.
2. **Next:** admit one focused FIX04 run on these hashes; a non-exact pass
   remains a breaker.
3. **Authority:** unchanged technical authority; all hash changes are local
   safety, compatibility, test-fixture or mechanical formatting changes.
4. **Discipline:** final sol-max review must reread the formatted hashes and
   cannot inherit the old component verdict by hash.

| Entry ID | Decision / approval | Commit / push | Gate evidence | Residual | Snapshot | State |
|---|---|---|---|---|---|---|
| MRG1-G1-C02-E22 | A04 C02; I41-I44 | `not-created`; forbidden | exact-8 lint/format/AST/diff PASS | focused FIX04 new-hash gate | MRG1-S47 | static GO / focused pending |

### 97.24 Two-node review-authority breaker

The final-formatted focused command ran once:

```text
.venv/bin/python -m pytest -q \
  tests/test_freecad_workbench_preview.py \
  tests/test_freecad_workbench_controller.py \
  -k 'fix04'

2 failed, 53 passed, 128 deselected in 16.10s
```

The two failures are:

1. exact same-cycle dual-token review is refused as
   `fresh shared review authority required`; and
2. the enqueue-failure node never reaches its synthetic signal failure
   because the same pre-enqueue review guard refuses first.

No full, C01, GUI or runtime gate ran afterward, and no process remained.
I45 read-only tracing proved that both preview tokens are correctly minted
before the fifth task-list response. That final response rebuilds the task
combo box: `clear()` emits `0 -> -1`, and the first `addItem()` emits
`-1 -> 0`. Dock therefore increments task-selection epoch twice even though
the final project, task, draft and generation are identical. The token-use
guard alone sees a different stamp and refuses.

Sol-high proposed tolerating only the last task-selection epoch at token use
while retaining it for in-flight response and mint correlation. Controller
has not admitted that change yet because it would tolerate a post-mint
task-selection ABA. An independent sol-max review is comparing that proposal
against the stricter alternative: suppress only programmatic combo-box
signals during final task-list rebuild, while retaining epoch changes for
real user selections and exact IDs for any actual rebuilt selection change.

#### MRG1-S48

1. **Completed:** the focused candidate improved from eight failures to two;
   all remaining failure evidence is one post-mint task-selector rebuild
   epoch mismatch.
2. **Next:** sol-max chooses the strictest local correction; tests must prove
   same-selection rebuild stability and real user task ABA invalidation
   before a new-hash focused gate.
3. **Authority:** standing technical authority covers selection freshness;
   no product/UI shape or public protocol change is contemplated.
4. **Discipline:** do not remove task-selection freshness globally, do not
   relax binding/generation/cycle checks and do not same-hash retry.

| Entry ID | Decision / approval | Commit / push | Gate evidence | Residual | Snapshot | State |
|---|---|---|---|---|---|---|
| MRG1-G1-C02-E23 | A04 C02; I45 recovery | `not-created`; forbidden | focused 2/53/128; exact event timeline | selection epoch correction + focused rerun | MRG1-S48 | NO-GO / adversarial adjudication |

### 97.25 Strict task-selection epoch recovery

Independent sol-max adjudication rejected both global removal of
task-selection epoch and a token-use-only prefix comparison. Either would
permit a user task `A -> B -> A` ABA to resurrect an old token.

Two tests were added first. The old candidate produced the exact expected
RED/PASS split:

```text
semantic same-task rebuild    FAIL: epoch 1 became 3
real task A -> B -> A         PASS: old token rejected
1 failed, 1 passed, 183 deselected
```

The correction blocks combo-box signals only during the synchronous,
programmatic final task-list rebuild. Signals are restored in `finally`
before returning. Exact project/task/draft/generation fields still reject a
rebuilt selection that actually changes, while real later user changes still
advance the unchanged `_task_changed()` epoch.

On the new hash:

```text
targeted semantic/ABA nodes   2 passed, 183 deselected
FIX04 focused                 57 passed, 128 deselected
```

Exact-eight Ruff, format, AST/compile and diff checks also passed.

#### MRG1-S49

1. **Completed:** same-selection refresh no longer creates a false epoch;
   real task ABA remains fail-closed with zero review side effect.
2. **Next:** run the complete Preview/Controller pair and stop on any legacy
   compatibility breaker.
3. **Authority:** private selection-freshness correction is covered by
   standing technical authority.
4. **Discipline:** Host retains full six-field stamp equality at receive,
   mint and consume; no token comparison was weakened.

| Entry ID | Decision / approval | Commit / push | Gate evidence | Residual | Snapshot | State |
|---|---|---|---|---|---|---|
| MRG1-G1-C02-E24 | A04 C02; strict task epoch | `not-created`; forbidden | targeted 2 PASS; FIX04 57 PASS; static PASS | complete pair | MRG1-S49 | focused GO / broad pending |

### 97.26 Broad-gate compatibility recovery

The first complete Preview/Controller gate exposed eight nodes and stopped
before C01:

```text
document-close retry convergence                         1
unstarted constructor residual                           1
partial-constructor wait assertion                       1
legacy raw cleanup observation                           5
```

Sol-high and sol-max independently classified two production lifecycle
defects and six stale test contracts:

1. a `closeDocument()` exception with an exactly unchanged registry was
   incorrectly marked sticky, so an exact later retry could never progress
   past Host's global recovery latch;
2. a deterministically never-started QThread residual was marked retired but
   not safely authorized for later Dock detachment;
3. the lexical assertion `"wait" not in Host.py` falsely matched the valid
   task status `awaiting_user_review`; and
4. five legacy nodes still expected hosted Dock to emit a raw restricted
   preview-close, which D33 deliberately forbids.

The six stale tests were migrated to an AST-level prohibition on `.wait()`
and to the current contract: raw Dock signal is exactly empty, while the
worker-thread private lane performs exact-once checkout cleanup. Two new
Preview nodes distinguish exact unchanged-registry close errors from
identity/delta drift. The existing rollback-close node was updated to the
same classification.

After correcting a three-field fake-client call assertion, the tests-only
candidate produced an exact clean RED:

```text
4 failed, 7 passed in 9.54s
```

The four failures were precisely Host close convergence, never-started
residual authorization, rollback non-sticky classification and discard
non-sticky classification.

A separate fake-host RED proved that `len(documents)+1` reused
`VibeCADPreview2` after partial retirement and could overwrite the surviving
document identity.

#### MRG1-S50

1. **Completed:** all broad-gate failures are deterministically classified;
   production lifecycle and fake identity defects have clean RED evidence.
2. **Next:** implement exact unchanged-registry retryability, start-attempt
   uncertainty and monotonic fake names, then rerun only the recovery packet.
3. **Authority:** these are lifecycle safety and test-contract corrections;
   no product or public protocol approval is required.
4. **Discipline:** any registry read/identity/delta uncertainty stays sticky;
   effect-then-raise may not be treated as success; raw hosted cleanup remains
   forbidden.

| Entry ID | Decision / approval | Commit / push | Gate evidence | Residual | Snapshot | State |
|---|---|---|---|---|---|---|
| MRG1-G1-C02-E25 | A04 C02; I52 recovery | `not-created`; forbidden | clean RED 4/7; fake-name RED 1 | I53 lifecycle implementation | MRG1-S50 | genuine RED / implementation authorized |

### 97.27 I53 lifecycle and fake-identity closure

I53 made three bounded corrections:

1. Preview classifies a close exception as non-sticky only when both registry
   reads succeed, the record was not already ambiguous and the complete
   key/object-identity registry is exactly unchanged. Every read failure,
   alias, replacement, addition, removal or other delta poisons the cycle and
   remains recovery-required. A post-effect exception is therefore sticky,
   including an exact-minus-target registry.
2. Host latches global recovery only for a `PreviewError` whose
   `recovery_required` flag is true. It also records
   `_thread_start_attempted` immediately before `QThread.start()`: synchronous
   pre-attempt construction can be safely retired, while effect-then-raise
   retains the session and grants no retirement authority.
3. the shared fake FreeCAD lazily seeds a monotonic document counter from the
   initial registry size and never reuses a retired name.

The first recovery run passed eleven of twelve nodes and found one additional
constructor-injection defect: Host used the module-global `ReviewDock`
constructor symbol to call static validators, but the test legitimately
wrapped that constructor with a factory. I55 now validates events and review
responses through the exact live Dock instance; a missing Dock rejects
fail-closed.

The final frozen hashes are:

```text
state.py       f9a3450b1645aa757141ffd51207a342da6c7971055ff184d561f286506fb895
gateway.py     070c4b73f82dc313e1570bb8a338cc5c8b173ca5cf4253bc7502f38f4edba27c
dock.py        3ba0b1c811c6acc03cde6fd9d1e52c07de3beac93caa9ab499bf9d64360a8ab8
host.py        e5f4f7ce8823c476f92c03bc78ce18ed1a73331a9c107926d27cf2ae9efb658f
preview.py     386fa977a8a3b6bdeecf7f1b21d2f986c24449c805b271145fe820189675080d
fake_host.py   f1adc449b7b2335aefa8b27e23728c8f2659b8927cca28ebcf42d8c0b7fa9622
controller     794a0055408968c765b281423d9946bb10c135781d7e24ae9a081766816b9f61
preview test   daf73e589bb630b84d9c9beddbe790faa2aa2b05a37742e82fce9871197526b8
```

On those exact bytes:

```text
recovery/adversarial packet   12 passed
FIX04 focused                 60 passed, 128 deselected
Ruff/format/AST/compile/diff  PASS
```

#### MRG1-S51

1. **Completed:** I53/I55 recovery nodes, fake identity and all sixty FIX04
   nodes pass on one static-clean hash.
2. **Next:** rerun the complete 188-node pair and C01 regression while a fresh
   sol-max final review independently rereads all eight paths.
3. **Authority:** unchanged A04 and standing technical authorization.
4. **Discipline:** V01 remains forbidden until both broad gates and exact
   `0/0/0/0` independent review close.

| Entry ID | Decision / approval | Commit / push | Gate evidence | Residual | Snapshot | State |
|---|---|---|---|---|---|---|
| MRG1-G1-C02-E26 | A04 C02; I53/I55 | `not-created`; forbidden | recovery 12 PASS; FIX04 60 PASS; static PASS | full pair, C01, final review | MRG1-S51 | focused GO / broad review active |

### 97.28 Clean broad-gate recertification

The first new-hash broad run returned functionally green results:

```text
Preview + Controller          188 passed
C01 non-slow                  233 passed, 1 deselected
```

Controller inspection then found an older sleeping pytest process from the
I54 twelve-node recovery command. Its exact PID, process group, parent,
command and repository cwd proved it was controller-owned task residue. It
was idle, had no child, and was retired by one exact `SIGTERM`; the current
gate process was not signalled.

Although the old process lived in a separate address space and the functional
results were green, its presence violated the zero-foreign-process preflight.
The first broad result is therefore recorded as informative, not final
certification.

I58 began from a process table containing no pytest, FreeCAD, freecadcmd or
VibeCAD daemon and reran both gates without changing bytes:

```text
.venv/bin/python -m pytest -q \
  tests/test_freecad_workbench_preview.py \
  tests/test_freecad_workbench_controller.py

188 passed in 38.39s

PYTHONPATH=src .venv/bin/python -m pytest -q \
  tests/test_freecad_workbench_package.py \
  tests/test_freecad_workbench_controller.py \
  tests/test_freecad_workbench_gui.py \
  -m 'not slow'

233 passed, 1 deselected in 39.03s
```

Each command was polled through its final process exit. The process table was
clean before the first gate, between gates and after the second. All eight
hashes, the empty index and exact HEAD/upstream remained unchanged.

#### MRG1-S52

1. **Completed:** C02 has clean-hash static, recovery, focused, full component
   and C01 regression PASS, including exact process retirement.
2. **Next:** fresh sol-max final review must return exact `0/0/0/0`; in
   parallel, read-only V01 preparation may finish but cannot launch.
3. **Authority:** unchanged A04 and standing technical authorization.
4. **Discipline:** I57 is not the final clean certificate; I58 is. V01,
   staging and commit remain forbidden until review closes.

| Entry ID | Decision / approval | Commit / push | Gate evidence | Residual | Snapshot | State |
|---|---|---|---|---|---|---|
| MRG1-G1-C02-E27 | A04 C02; I58 | `not-created`; forbidden | 188 PASS; C01 233+1; exact process/hash/index/refs | final sol-max review, then V01 | MRG1-S52 | mechanical GO / review active |

### 97.29 I57B final adversarial review

The fresh sol-max review rejected the I58 candidate with:

```text
critical  0
major     3
moderate  1
minor     0
```

The prior static and mechanical gates remain useful regression evidence, but
they are not an implementation admission certificate. V01 remains forbidden.

The three Major findings are independent:

1. active-session preview cleanup allocates from the reserved high cleanup-ID
   lane. Gateway advances one global request high-water to that ID while Dock's
   normal cursor remains low, so every later refresh/open in the same session
   is rejected as stale;
2. an exact-no-effect rollback close exception can remain non-sticky while
   retaining an old document and checkout authority. A real client can return
   new unique checkout IDs, and Host currently mints and consumes review tokens
   without requiring cycle-wide aggregate eligibility; and
3. transport exhaustion unconditionally sets Host's global recovery latch
   even when `PreviewError.recovery_required` is false, preventing an exact
   later cleanup retry from retiring the client and thread.

The Moderate finding is an ambiguous review enqueue exception. Review pending
is committed before signal emission, but an exception does not move the active
session to a fail-closed terminal state. A later refresh can therefore mint
new tokens and attempt a second mutation while the first outcome is unknown.

I59 is tests-first. It must prove:

- active cleanup uses the shared normal monotonic sequence and later business
  traffic remains usable, while the reserved high lane is terminal only;
- aggregate eligibility rejects any extra or unbound owned authority, token
  mint and token consume both require that aggregate, and non-sticky rollback
  failure makes the cycle drain before any review;
- transport exhaustion distinguishes retryable and sticky document-close
  failures; and
- any review enqueue ambiguity consumes the tokens and cannot remain an active
  session capable of another review.

#### MRG1-S53

1. **Completed:** I57B is frozen as a `0/3/1/0` NO-GO with four bounded
   production paths and no public protocol or product-shape change.
2. **Next:** obtain genuine tests-only RED on the unchanged production hash,
   then use one sol-high writer for the bounded correction.
3. **Authority:** standing technical authority covers lifecycle, replay and
   review-authority fail-closed fixes; no product approval is required.
4. **Discipline:** V01, staging and commit remain forbidden; the same candidate
   cannot regain admission without new-hash gates and a fresh `0/0/0/0`.

| Entry ID | Decision / approval | Commit / push | Gate evidence | Residual | Snapshot | State |
|---|---|---|---|---|---|---|
| MRG1-G1-C02-E28 | A04 C02; I57B | `not-created`; forbidden | sol-max `0C/3M/1Mo/0m` | I59 RED and correction | MRG1-S53 | NO-GO / V01 HOLD |

### 97.30 V01 read-only preparation

The independent sol-max V01 preparation found no product, public-fixture or
repository-harness blocker. It did not launch FreeCAD or a daemon and did not
write a probe.

The one-shot fixture can be created entirely through public APIs:

1. create one empty project;
2. run an `auto_commit` `create_box` task to produce a valid FCStd HEAD; and
3. run a `require_review` `create_box` task to produce one exact
   `awaiting_user_review` draft.

The eventual probe remains repository-external and hash-frozen. It will use
owner-private isolated VibeCAD and FreeCAD roots, bind the exact managed
runtime generation and executable identities, consume one candidate-specific
launch marker, and start one GUI process only. In that GUI it will:

- prove queued `Signal(object)` object identity, FIFO ordering and main/worker
  thread identities for consecutive HEAD and Draft opens;
- retain exact source, open-key, descriptor, grant, claim, path hash/size,
  document Name/object identity and `Modified is False` evidence;
- require two distinct managed documents and a connected/live/clean Dock;
- capture one screenshot with no absolute user or isolated path exposed; and
- prove document -> checkout -> client -> worker/Dock -> authenticated daemon
  retirement, followed by zero process/socket/receipt/checkout residue.

Any observation defect is a V01 failure. The candidate-specific consumed
marker prohibits retry-until-green. Admission remains conditional on exact
new-hash static/mechanical gates, a clean process table and final sol-max
`0/0/0/0`.

#### MRG1-S54

1. **Completed:** V01 has a public-fixture, one-shot process, machine-evidence,
   screenshot and exact-cleanup plan with no repository code requirement.
2. **Next:** keep the prepared plan dormant through I59 implementation and
   all new-hash gates.
3. **Authority:** preparation is read-only; the already approved A04 scope
   covers the eventual technical validation launch after admission.
4. **Discipline:** no FreeCAD/daemon launch, fixture creation or consumed
   marker exists yet.

| Entry ID | Decision / approval | Commit / push | Gate evidence | Residual | Snapshot | State |
|---|---|---|---|---|---|---|
| MRG1-G1-C02-E29 | A04 C02; V01 prep | `not-created`; forbidden | sol-max read-only conditional GO | exact `0/0/0/0` admission | MRG1-S54 | PREPARED / HOLD |

### 97.31 I59 tests-only RED

I59 added six selectors expanding to eight cases without changing production.
The fake local client gained an opt-in monotonic checkout/grant mode so the
test can model real unique authorities; its default behavior remains
unchanged.

On the unchanged production hashes:

```text
I59 selectors                    6 failed, 2 passed
fixture compatibility packet    3 passed
Ruff/format/AST/compile/diff     PASS
```

The two passing cases are intentional anti-overcorrection controls:

- after a terminal high-ID request, lower normal business IDs remain rejected;
- a document close that took effect before raising remains sticky and does not
  progress to checkout or client cleanup.

The six failures precisely prove the four review findings:

1. both stale-success and validation-failure active cleanup used request ID
   `9007199254739968`; the later normal open used ID `4` and was rejected;
2. one retained unbound authority plus two new unique bindings produced
   aggregate eligibility, minted tokens and one daemon review instead of
   rejecting all five observations;
3. retryable transport-exhaustion cleanup closed documents and checkouts on
   the second attempt but the unconditional latch retained the client in
   `stopping`; and
4. a pre-effect review emit exception retained committed pending while still
   active, while an effect-then-raise performed exactly one mutation but also
   stayed active and allowed a later refresh.

Production cannot infer from a generic signal exception whether the queued
emit had no effect or took effect before raising. I60 therefore freezes the
conservative contract: every such review exception retains the correlated
pending authority and immediately stops new business. An exact authenticated
success may resolve that authority and permit terminal cleanup; an
`unknown_outcome` or absent result retains the session fail-closed.

#### MRG1-S55

1. **Completed:** all four I57B findings have stable tests-only RED and two
   explicit sticky/terminal controls.
2. **Next:** implement the bounded Host/Preview correction with one sol-high
   writer and rerun the eight cases plus the complete FIX04 packet.
3. **Authority:** unchanged standing technical authority.
4. **Discipline:** no generic emit exception may be guessed pre-effect; active
   cleanup and terminal cleanup must remain separate ID lanes.

| Entry ID | Decision / approval | Commit / push | Gate evidence | Residual | Snapshot | State |
|---|---|---|---|---|---|---|
| MRG1-G1-C02-E30 | A04 C02; I59 RED | `not-created`; forbidden | 6 RED / 2 anti-overcorrection PASS; fixture/static PASS | I60 implementation | MRG1-S55 | genuine RED / implementation active |

### 97.32 I60 authority and lifecycle correction

I60 changed only Host, Preview and the related Controller tests after the
tests-only RED.

Host now separates request lanes by lifecycle:

- an authenticated private `preview_close` while the session remains active
  consumes and advances Dock's shared normal monotonic cursor;
- stopping and terminal cleanup continue to use the reserved high-ID lane;
  and
- once the high lane is used, no lower normal business request is admitted.

Preview aggregate eligibility now requires an exact identity bijection:

```text
cycle authorities  2
cycle bindings     head + draft
global bindings    2
```

Every authority must be live, document-open, checkout-open and unambiguous,
with the exact same record, source, descriptor, document and binding object in
the authority, cycle and global collections. Any extra or unbound authority
rejects the aggregate. Host checks the aggregate at both review-token mint and
review-token consume. An exact-no-effect rollback close failure marks its
cycle draining without inventing a global sticky recovery outcome.

Transport exhaustion now latches global recovery only when the caught
`PreviewError` is recovery-required. A retryable exact-no-effect close remains
stopping and can converge on an explicit retry; effect-then-raise remains
sticky.

Review enqueue exceptions use one request-bound ambiguity marker. Because a
generic signal exception cannot prove whether the queued emit took effect,
Host always retains the committed pending request, consumes the tokens and
enters stopping. The marker explicitly blocks client close and thread
retirement. An exact authenticated success first retires the correlated
pending request, clears only that marker and invokes `close_async()` so the
result itself drives document -> checkout -> client -> inactive. Missing,
malformed, mismatched or `unknown_outcome` evidence remains fail-closed.

Implementation gates on the new bytes:

```text
I59 selectors                     8 passed
I59 + old enqueue + shape         10 passed
FIX04 focused                     69 passed, 128 deselected
Preview + Controller full         197 passed
Ruff/format/compile/diff          PASS
```

A bounded independent sol-max architecture review reread the implementation
and returned `0C/0M/0Mo/0m`. It confirmed the lane split, exact aggregate
bijection, draining semantics, conditional exhaustion latch, request-bound
ambiguity marker, explicit terminal guards and success-driven cleanup. This is
an architecture GO, not V01 admission; independent mechanical and final full
adversarial gates remain.

Current exact-eight hashes:

```text
state.py       f9a3450b1645aa757141ffd51207a342da6c7971055ff184d561f286506fb895
gateway.py     070c4b73f82dc313e1570bb8a338cc5c8b173ca5cf4253bc7502f38f4edba27c
dock.py        3ba0b1c811c6acc03cde6fd9d1e52c07de3beac93caa9ab499bf9d64360a8ab8
host.py        4ddf124b29a6f72ff6b5ab510cbca6d56d0d3ff0a81c1760cc937ade4d00abdc
preview.py     8f3b4714be4ec2dd2aa8006c49e6560ba187578d65d0916be368ed62825b7967
fake_host.py   fb152061f6b6b5a92fcb5ee2b3c71425e7123d863f4f73f2688b7e7ed04a38e8
controller     cd5919a11976df2c0ae80a2c35379a2d9d4dced44bd45b872a93816468752918
preview test   daf73e589bb630b84d9c9beddbe790faa2aa2b05a37742e82fce9871197526b8
```

#### MRG1-S56

1. **Completed:** all I59 cases, the complete FIX04 packet and the 197-node
   component pair pass; bounded architecture review is clean.
2. **Next:** independent terra-medium static/component/C01 recertification on
   these exact hashes, followed by a fresh full sol-max review.
3. **Authority:** unchanged standing technical authority.
4. **Discipline:** architecture GO is not final review admission; V01,
   staging and commit remain forbidden.

| Entry ID | Decision / approval | Commit / push | Gate evidence | Residual | Snapshot | State |
|---|---|---|---|---|---|---|
| MRG1-G1-C02-E31 | A04 C02; I60 | `not-created`; forbidden | I59 8; FIX04 69; full 197; arch `0/0/0/0` | independent mechanical + final review | MRG1-S56 | implementation GO / V01 HOLD |

### 97.33 I61/I62 active-cleanup exhaustion recovery

During I61 independent mechanical recertification, controller review found one
additional active-lane exhaustion path. I61 was stopped after its already
launched commands exited naturally; its static, I59 and FIX04 results are
informative only, and its complete-suite output was not accepted as gate
evidence.

The defect is a narrow exception-boundary error:

1. active cleanup selects the shared normal allocator;
2. an exhausted Dock normal cursor invokes `_transport_exhausted()`;
3. that method correctly changes lifecycle to stopping and, when document
   state is exact, leaves global recovery false; but
4. `_advance_cleanup()` catches its `ProjectionError` and unconditionally
   turns the controlled terminal transition into permanent recovery.

I62 added one two-sided parameterized selector. On unchanged I60 production:

```text
active normal cursor exhausted    FAIL: recovery was true, expected false
terminal high cursor exhausted    PASS: sticky session retained
1 failed, 1 passed
```

The frozen correction separates reservation from enqueue errors. Only an
active-normal reservation whose controlled exhaustion has already changed the
session to stopping and left recovery false may return without adding a
sticky latch. A later explicit close uses the terminal high lane. Enqueue
ambiguity, non-Projection reservation failure, terminal high-lane exhaustion
and any already-sticky state remain recovery-required.

#### MRG1-S57

1. **Completed:** the active-normal exhaustion path has genuine RED and a
   terminal high-exhaustion anti-overcorrection PASS.
2. **Next:** implement the exact exception split, rerun the new selector and
   invalidate/repeat all broad gates on the new Host hash.
3. **Authority:** unchanged standing technical authority.
4. **Discipline:** no recursive or automatic retry; terminal high-lane failure
   must never be reclassified as retryable.

| Entry ID | Decision / approval | Commit / push | Gate evidence | Residual | Snapshot | State |
|---|---|---|---|---|---|---|
| MRG1-G1-C02-E32 | A04 C02; I62 RED | `not-created`; forbidden | active RED 1 / terminal control PASS 1 | I63 fix and full recertification | MRG1-S57 | genuine RED / V01 HOLD |

### 97.34 I63 cleanup reservation boundary

I63 changed only Host's ready-checkout cleanup branch. It snapshots whether
the attempt selected the active normal allocator and separates reservation
from enqueue handling.

The only non-sticky exception is now:

```text
allocator selected while active
and reservation raised ProjectionError
and lifecycle is now stopping
and global recovery remains false
```

That exact state means `_transport_exhausted()` already performed a controlled
terminal transition. The method returns without recursion or automatic retry;
an explicit close can then use the reserved high lane. Every terminal/high
reservation error, active reservation with a sticky recovery result,
non-Projection reservation error and enqueue exception remains sticky.

New-hash implementation results:

```text
I62 lifecycle-sensitive selector     2 passed
I59 + old enqueue + marker shape     10 passed
FIX04 focused                        71 passed, 128 deselected
Preview + Controller full            199 passed
Ruff/format/compile/diff             PASS
```

Current exact-eight hashes:

```text
state.py       f9a3450b1645aa757141ffd51207a342da6c7971055ff184d561f286506fb895
gateway.py     070c4b73f82dc313e1570bb8a338cc5c8b173ca5cf4253bc7502f38f4edba27c
dock.py        3ba0b1c811c6acc03cde6fd9d1e52c07de3beac93caa9ab499bf9d64360a8ab8
host.py        25cbadcc9b5c317bde84292986c3672f30bc1a15a3647d165cf3333d973e3eb1
preview.py     8f3b4714be4ec2dd2aa8006c49e6560ba187578d65d0916be368ed62825b7967
fake_host.py   fb152061f6b6b5a92fcb5ee2b3c71425e7123d863f4f73f2688b7e7ed04a38e8
controller     6559c0e777d5ad725e25942c02c0ac2cc013bfe8327e5c4d55dd42efe81a364d
preview test   daf73e589bb630b84d9c9beddbe790faa2aa2b05a37742e82fce9871197526b8
```

#### MRG1-S58

1. **Completed:** active-normal and terminal-high exhaustion are correctly
   separated; all focused and 199 component nodes pass.
2. **Next:** rerun independent static/component/C01 certification on S58
   hashes, then perform a new full sol-max adversarial review.
3. **Authority:** unchanged standing technical authority.
4. **Discipline:** all I61 broad evidence is superseded; I64 must start from a
   clean process table and retain final command summaries.

| Entry ID | Decision / approval | Commit / push | Gate evidence | Residual | Snapshot | State |
|---|---|---|---|---|---|---|
| MRG1-G1-C02-E33 | A04 C02; I63 | `not-created`; forbidden | I62 2; FIX04 71; full 199; static PASS | I64 mechanical + final review | MRG1-S58 | implementation GO / V01 HOLD |

### 97.35 I64/I65 independent mechanical certification

I64 independently confirmed, without changing bytes:

```text
Ruff check/format, AST, compile, diff    PASS
I62 lifecycle exhaustion                 2 passed
I59 + enqueue + marker                   10 passed
FIX04 focused                            65 passed, 75 deselected
```

Its first broad invocations exited with no displayed failure and left a clean
process table, but the execution transport lost both final summaries and exit
codes. Those results were not accepted as certification.

I65 was a new clean recertification, not a retry after a test failure. It used
direct process sessions and polled each original command through final exit:

```text
.venv/bin/python -m pytest -q \
  tests/test_freecad_workbench_preview.py \
  tests/test_freecad_workbench_controller.py

199 passed in 45.97s
exit 0

PYTHONPATH=src .venv/bin/python -m pytest -q \
  tests/test_freecad_workbench_package.py \
  tests/test_freecad_workbench_controller.py \
  tests/test_freecad_workbench_gui.py \
  -m 'not slow'

244 passed, 1 deselected in 45.27s
exit 0
```

Relevant process tables were clean before and after recertification. The exact
eight hashes, empty index and HEAD/upstream remained unchanged.

#### MRG1-S59

1. **Completed:** S58 bytes have independent static, focused, 199-component
   and C01 `244+1` PASS with complete exit and process-retirement evidence.
2. **Next:** both parallel sol-max final adversarial reviews must finish and
   synthesize to exact `0/0/0/0`.
3. **Authority:** unchanged standing technical authority.
4. **Discipline:** no broad output without final summary/exit code is gate
   evidence; V01 remains frozen pending final review.

| Entry ID | Decision / approval | Commit / push | Gate evidence | Residual | Snapshot | State |
|---|---|---|---|---|---|---|
| MRG1-G1-C02-E34 | A04 C02; I65 | `not-created`; forbidden | static/focused PASS; full 199; C01 244+1; clean processes | two final sol-max reviews | MRG1-S59 | mechanical GO / V01 HOLD |

### 97.36 I64 parallel final adversarial verdict

Two independent sol-max reviewers reread the exact S59 hashes.

The Host/Gateway/Dock reviewer returned exact `0/0/0/0`. It found no breaker
in request high-water/replay, normal versus terminal lanes, reserve/enqueue,
private wrapper correlation, review ambiguity, lifecycle retirement or
document -> checkout -> client ordering. It retained real PySide
`Signal(object)` identity/FIFO as a V01 evidence obligation.

The Preview/authority reviewer returned `0C/2M/0Mo/0m`, making the combined
verdict NO-GO:

1. Dock retains an open HEAD when project selection changes. A Draft from the
   new project can then form a structurally valid head/draft pair because
   aggregate eligibility does not require a common resolved project, and Host
   does not require that common project to equal the current Dock project.
2. Final local observation checks document registry identity, Name and
   `Modified is False`, but a same-path FreeCAD save clears Modified. After a
   clean refresh minted tokens, the user can therefore modify and save the
   file, keep the same document identity and consume an old clean descriptor
   while viewing bytes that differ from the reviewed Draft. Save As similarly
   changes the document file path without an existing path check.

The earlier candidate that closed unbound authority remains recorded until
full-cycle retirement was withdrawn: D33 explicitly requires same-cycle
history and full exact retirement, so it is not a defect.

I66 must first prove both remaining paths on frozen production. The correction
must require:

- exact HEAD/Draft resolved-project equality, agreement with the requested
  HEAD project and agreement with Dock's current project at token mint and
  consume; and
- document `FileName` equality with the claimed managed path plus final
  size/SHA-256 equality across claim, binding descriptor and fresh token.

Unchanged clean files must still review once. Same-path save, Save As and
external same-path rewriting must produce zero review side effect and poison
the affected authority.

#### MRG1-S60

1. **Completed:** S59 mechanical evidence is clean, Host final review is
   clean, and two remaining Preview admission breakers are fully traced.
2. **Next:** tests-only cross-project and file-attestation RED on the unchanged
   S59 production hashes.
3. **Authority:** both fixes enforce the frozen comparison/review semantics;
   no public protocol, UI or product-shape approval is required.
4. **Discipline:** do not treat full-cycle retirement as a liveness defect;
   do not trust `Modified=False` as a content digest.

| Entry ID | Decision / approval | Commit / push | Gate evidence | Residual | Snapshot | State |
|---|---|---|---|---|---|---|
| MRG1-G1-C02-E35 | A04 C02; I64 final review | `not-created`; forbidden | Host `0/0/0/0`; Preview `0/2/0/0` | I66 tests-only RED | MRG1-S60 | NO-GO / V01 HOLD |

### 97.37 I66 project/file-observation RED

I66 changed test infrastructure only. Production remained frozen at the S59
hashes while two selectors exercised the exact final-review findings.

The cross-project selector opens a HEAD resolved for project A, changes the
Dock selection, then opens a Draft resolved for project B. On unchanged
production the mixed pair incorrectly remains aggregate-eligible, mints review
authority and invokes one authenticated review. Its anti-overcorrection
control performs full exact retirement, then opens a clean B-HEAD/B-Draft pair
and reviews exactly once.

The file-observation selector materializes an actual checkout file and uses the
real claim path, size and SHA-256. Its unchanged control passes. Three
post-refresh drifts all incorrectly retain review authority:

```text
cross-project A-HEAD + B-Draft       FAIL: one review side effect
unchanged exact file                 PASS: one review side effect
same-path save/reset                 FAIL: one review side effect
Save As                              FAIL: one review side effect
external same-path rewrite           FAIL: one review side effect

4 failed, 1 passed in 6.17s
```

The observed direction, magnitude and path match the I64 prediction, so this
is accepted as genuine RED rather than a setup failure. Static checks passed;
no production file, D33 assertion, index entry or ref changed.

New tests-only hashes:

```text
fake_host.py   8b903ebdbdefc2e691c859045b8a5ff9f9181a509f5aef11e10ea03feab7cab3
controller     e13d5e391e4fb958a04c001769a2f91363218a12fe753293db6c0eca779290be
preview test   daf73e589bb630b84d9c9beddbe790faa2aa2b05a37742e82fce9871197526b8
```

I67 is one strongly coupled correction packet. It must require one exact
resolved/current project at eligibility, mint and consume, and must bind the
live document path plus freshly attested file size/SHA-256 before any review
effect. A failed final observation poisons the affected authority. Missing
test paths cannot create a production bypass; any compatibility seam remains
test-only, while the I66 materialized selector exercises the real attestor.

#### MRG1-S61

1. **Completed:** both remaining Major findings have prediction-matching RED;
   the unchanged and clean same-project controls preserve liveness.
2. **Next:** I67 minimal production correction, then all focused, full
   component and C01 gates on the new exact hashes.
3. **Authority:** standing technical authorization covers this frozen-semantic
   correctness/security fix; no product/UI/public-protocol change is allowed.
4. **Discipline:** one writer owns the coupled Preview/Host/Dock correction;
   V01, staging, commit and push remain controller-forbidden until fresh
   independent mechanical and sol-max review are green.

| Entry ID | Decision / approval | Commit / push | Gate evidence | Residual | Snapshot | State |
|---|---|---|---|---|---|---|
| MRG1-G1-C02-E36 | A04 C02; I66 RED | `not-created`; forbidden | exact selector `4 failed, 1 passed`; static PASS | I67 correction + recertification | MRG1-S61 | genuine RED / V01 HOLD |

### 97.38 I67 exact-project and final-file correction

I67 changed only Dock, Host, Preview and their three test/fixture paths.
It preserves the frozen public protocol and product shape.

Project authority now has three layers:

1. a project-selection change synchronously revokes projected review
   eligibility;
2. aggregate authority requires the HEAD and Draft resolved projects to be
   identical and that project to equal the requested HEAD project; and
3. both token mint and final consumption require the shared project, Dock
   current project and selected task project to be identical.

Final review consumption now re-observes the exact document path and claimed
file. Claim, retained binding descriptor and fresh token must agree on path,
size and SHA-256. The claimed path is then opened read-only with close-on-exec
and no-follow where supported. The attestor requires a regular file owned by
the current effective user, one hard link, an exact bounded size, a stable
pre/post stat identity, a path entry that still resolves to the opened
device/inode and a constant-time SHA-256 match. Every observation failure is
normalized to fail-closed `PreviewError`, poisons the affected cycle and
occurs before review request reservation or enqueue.

An initial implementation used a test-module-name branch to treat a missing
fake file as attested. Parallel sol-max pre-review rejected that production
path even though it was test-named. I67 removed it entirely. The legacy fake
client now materializes real files in owned temporary checkout roots and
derives their actual descriptors; the I66 materialized selector and all final
attestation paths therefore use the same production FD/hash implementation.

Coverage added during pre-review also proves:

- a previously true same-project projection becomes false immediately on a
  project switch;
- same-size, different-content rewrites require SHA-256 rather than only
  length;
- missing-file errors are normalized and cannot preserve reusable tokens;
- symlinks and hard links fail while one exact regular file passes; and
- failed final observation still converges through full D33
  document -> checkout -> client retirement.

Implementer gates on the new exact hashes:

```text
I67 targeted                         10 passed in 3.95s
FIX04                                81 passed, 128 deselected in 29.59s
Preview + Controller full            209 passed in 49.75s
py_compile/Ruff check+format/diff     PASS
```

Current exact-eight hashes:

```text
state.py       f9a3450b1645aa757141ffd51207a342da6c7971055ff184d561f286506fb895
gateway.py     070c4b73f82dc313e1570bb8a338cc5c8b173ca5cf4253bc7502f38f4edba27c
dock.py        6ed16de38f7ca8f154db5100a6d4cadff263143d803795b7e314aa5012e92b57
host.py        5e064e03b442c1353116bd2206507fa56608530a7c7f58f214a547e73cb96556
preview.py     dd6cf6d80ec933b485083ba4acc9602b8530d3a47978e88e4358a28fc349973f
fake_host.py   1840f0a2ed6f907c4af7538fb9252c95396b63b2ea344c7b05b422bd6a1a8e25
controller     f919bc31e9754b2094715cc7311586fb3a75d3485588721facca1ca714ae4d78
preview test   e15597e92880276ca9c9378e7433681766849ee0db268700324690e95c409985
```

#### MRG1-S62

1. **Completed:** I66 is GREEN, the production missing-file test bypass was
   removed, FIX04 has 81 PASS and the component pair has 209 PASS.
2. **Next:** independent terra-medium static/component/C01 certification and
   two fresh sol-max final reviews on the exact S62 hashes.
3. **Authority:** unchanged standing technical authority.
4. **Discipline:** implementer evidence is not independent admission; V01,
   staging, commit and push remain frozen until mechanical and both final
   reviews are accepted.

| Entry ID | Decision / approval | Commit / push | Gate evidence | Residual | Snapshot | State |
|---|---|---|---|---|---|---|
| MRG1-G1-C02-E37 | A04 C02; I67 | `not-created`; forbidden | targeted 10; FIX04 81; full 209; static PASS | I68 mechanical + I69 final reviews | MRG1-S62 | implementation GO / V01 HOLD |

### 97.39 I68-I79 proof closure and final automated admission

I68 and I71 are preserved as non-certifying mechanical attempts. Both passed
their completed static and targeted gates, but their agents invoked long
commands through a nested execution cell. That cell reported completion before
the child pytest process had exited. I68 briefly overlapped a second component
invocation before the ambiguity was observed; I71 stopped without relaunching.
Neither result is used as broad gate evidence, and neither continued to C01.
All involved processes later retired with no residue.

Post-I67 sol-max review then identified two proof-strength gaps:

1. the direct symlink/hardlink attestor test asserted aggregate ineligibility
   with only one binding, which was tautologically false even without poison;
2. selected-task project equality had no isolated drift regression.

I70 changed tests only. It directly asserts that the regular-file control
leaves the active cycle clean while symlink and hardlink rejection poison it.
It also creates a clean dual-token pair, changes only the frozen selected
task's `project_id`, and proves zero command/effect plus D33 retirement.
Its first test construction treated frozen `TaskSummary` as a dictionary and
produced `3 passed, 1 failed`; this setup error is retained as I70-A. A bounded
`dataclasses.replace` correction preserved every field except `project_id` and
made the focused selector `4 passed`.

That stronger test exposed one real remaining Moderate. Selected-task project
drift returned before Host's existing poison catch. Review was blocked and
tokens were cleared, but the cycle was not sticky-poisoned. I75 added a genuine
RED:

```text
selected-task project-only drift
zero command/effect and token clearing     PASS
cycle.poisoned is True                     FAIL (observed False)
1 failed in 1.89s
```

The minimal correction leaves ordinary task ID, Draft, generation and status
early returns unchanged. Only invalid current-project identity and selected
task project mismatch now enter the existing aggregate-authority exception
path, which poisons both exact token bindings before returning false. The test
then proves a subsequent two-descriptor refresh cannot re-mint tokens or
eligibility on the same poisoned cycle, followed by exact D33 retirement.

I75 implementation gates:

```text
I75 selector                           1 passed in 0.11s
adjacent project/file selectors        6 passed in 3.25s
FIX04                                  82 passed, 128 deselected in 25.31s
py_compile/Ruff check+format/diff       PASS
```

Two fresh sol-max final reviews independently returned exact
`0C/0M/0Mo/0m`. They confirmed the project predicate's narrow placement,
two-binding poison, zero pre-effect behavior, no-remint stickiness,
non-recovery cleanup and all earlier cross-project/final-FD conclusions.

I79 supersedes I68/I71 and used direct native process sessions only. Each long
pytest was launched once, its original session was polled through final exit,
and a subsequent process check was empty:

```text
Ruff check/format, py_compile, diff      PASS
I67/I70/I75 targeted                     10 passed in 4.02s
Preview + Controller full                210 passed in 45.32s
C01 non-slow package/controller/gui      251 passed, 1 deselected in 45.66s
final relevant process table             empty
```

Initial and final refs, empty index and exact hashes all matched:

```text
state.py       f9a3450b1645aa757141ffd51207a342da6c7971055ff184d561f286506fb895
gateway.py     070c4b73f82dc313e1570bb8a338cc5c8b173ca5cf4253bc7502f38f4edba27c
dock.py        6ed16de38f7ca8f154db5100a6d4cadff263143d803795b7e314aa5012e92b57
host.py        022ba352386fe264a988bc9ef1abd61d60ac8cde726a0e7ba30852c4208385b5
preview.py     dd6cf6d80ec933b485083ba4acc9602b8530d3a47978e88e4358a28fc349973f
fake_host.py   1840f0a2ed6f907c4af7538fb9252c95396b63b2ea344c7b05b422bd6a1a8e25
controller     f304efd2c6a344ed0356fc1c083875d207a8437a44f808c33ad84a8195deccf8
preview test   209ff75269cce2fd57eb021819a3b3ae27b7a9f4b077a47a147356c266130bc1
```

#### MRG1-S63

1. **Completed:** both final adversarial reviews are exact `0/0/0/0`;
   independent targeted, 210-component and C01 `251+1` gates have complete
   native-session exits and clean process evidence.
2. **Next:** run exactly one frozen V01 real FreeCAD/PySide acceptance probe,
   then capture evidence and retire all documents, checkouts, client and
   process residue.
3. **Authority:** prior MRG1-A04/A05/A06 plus standing technical gate
   authorization cover V01; no product decision or shape change is involved.
4. **Discipline:** V01 may not expand beyond the frozen public fixture and
   one-shot probe; any unexpected runtime/environment result stops acceptance.
   No staging, commit or push until V01 evidence is archived.

| Entry ID | Decision / approval | Commit / push | Gate evidence | Residual | Snapshot | State |
|---|---|---|---|---|---|---|
| MRG1-G1-C02-E38 | A04 C02; I68/I71 | `not-created`; forbidden | partial static/targeted PASS; broad process ambiguity | superseded by I79 | MRG1-S63 | non-certified / superseded |
| MRG1-G1-C02-E39 | A04 C02; I70/I75 | `not-created`; forbidden | I70-A setup fail retained; I75 RED; selector 1; adjacent 6; FIX04 82 | none | MRG1-S63 | correction complete |
| MRG1-G1-C02-E40 | A04 C02; I77-I79 | `not-created`; forbidden | reviews both `0/0/0/0`; targeted 10; full 210; C01 251+1 | V01 real runtime | MRG1-S63 | automated GO / V01 admitted |

### 97.40 I80-I90 post-S63 closure and terminal V01 evidence

S63 admitted one V01 invocation, but the candidate and controller needed three
further proof corrections before that invocation was consumed.

I80 retained the genuine draft fixture mismatch found by the first final
controller review. A real Draft checkout reports the base HEAD in
`source_head`, while its resolved revision and manifest identify the candidate.
The old parser required all three to be the same. The selector
`test_draft_open_accepts_candidate_revision_distinct_from_base_head` was RED
at the production descriptor parser. FIX05 made revision/manifest
self-equality HEAD-only and preserved project, model, size, hash and task
authority for Draft. The focused selector, strict HEAD selector, Preview full
64 and Preview + Controller full 211 were GREEN.

I81-I82 corrected the frozen V01 controller before any real launch:

- Draft evidence now cross-validates its `source_head` against the fixture
  HEAD/base rather than the candidate revision;
- `proc_listpids` sizing and fill reads clear and inspect `errno`;
- already-observed process generations that change PGID or SID are treated as
  escaped, never absent; and
- evidence creation always uses exclusive create, so an existing evidence
  path is terminal rather than silently skipped.

The exact controller-context managed-runtime authentication subsequently
returned true. Python 3.12, FreeCAD 1.1.0, VibeCAD 0.6.0, server epoch 4,
MCP 1.27.2 and the 28-tool public-surface digest
`ae495ba457af40a5837a03e77eef4b396b0a4209755878350bc341ac7de8bfd3`
all matched. No runtime install, refresh or rebuild was performed.

I83 retained one independent component failure:

```text
test_fix04_transport_exhaustion_close_retry_is_effect_sensitive[no-effect]
1 failed, 210 passed in 55.93s
post-summary pytest process did not retire
```

The failure was a test-harness lifecycle race. Its one-second global event
pump crossed several worker/main round trips before the explicit worker
barrier, and exceptional cleanup neither isolated events by session nor
required its worker to retire. FIX06 added a genuine RED for retained
session-owned events, then replaced that wall-clock race with bounded FIFO
worker/main settlement and exact session-owned cleanup. It did not increase a
timeout or change production behavior.

The first post-FIX06 sol-max closure review returned
`0 Critical / 2 Major / 1 Moderate / 0 minor`:

1. Draft `source_head` was not an exact five-field match for HEAD, and the
   opened Draft candidate/base revisions were not bound to the selected task;
2. a child that escaped the original PGID/SID before its first census could
   survive while controller evidence reported clean; and
3. fake failure cleanup could consume and suppress a foreign session's main
   event.

FIX07 closed all three with genuine counterexamples. Aggregate Preview
authority now requires exact HEAD/Draft `source_head` equality. Token mint and
final review consumption both bind Draft resolved revision to
`selected_task.candidate_revision` and Draft `source_head.revision_id` to
`selected_task.base_revision`; any drift poisons both bindings, clears
authority and produces zero review effect. Fake cleanup is owner-scoped and
preserves foreign events. The V01 controller snapshots same-EUID process
generations before launch and treats every unproven post-baseline generation
as ambiguous failure without signaling it.

The final independent gates on the exact FIX07 bytes were:

```text
Preview + Controller full                217 passed in 2.05s
C01 non-slow package/controller/gui      257 passed, 1 deselected in 2.61s
Ruff check/format, py_compile, diff       PASS
final sol-max review                     0/0/0/0; exactly-one-V01 GO
final terra preflight                    PASS
```

FIX07 exact-eight hashes:

```text
state.py       f9a3450b1645aa757141ffd51207a342da6c7971055ff184d561f286506fb895
gateway.py     070c4b73f82dc313e1570bb8a338cc5c8b173ca5cf4253bc7502f38f4edba27c
dock.py        6ed16de38f7ca8f154db5100a6d4cadff263143d803795b7e314aa5012e92b57
host.py        525182eea17a73d9d03dc8ddd06474a028364345e82dc18849e2d9fb7e371fc4
preview.py     c122bf05a8830b04bc4e62dbc84e4986b4ee58f40f4be260ef88fae15a5b9130
fake_host.py   4d102652bb54afa19a6eff053e9ab5068fb74ba5b6edb594960e3bb4039bec24
controller     420522cabc6cda76ce8ede79ee34bd40c60c235dcf7ed3cde55f215bde7125d7
preview test   c566a09edcc7caf4d49aa8ff726100238eefcc0a2ab36e34529447b39b63d048
```

The admitted V01 controller was frozen at
`15977b6b9cc10c535ba8c0f47c3b8a5df924ebb4afa01987b4edd2d0d2a67660`;
the probe remained
`31d039fdd38febf4c86b340aef9f126a0b68d7cf189fc1f32b82fd456812e93c`.
Its one and only invocation exited 1 before GUI launch:

```text
status                            failed
controller launch count           0
GUI process token                 absent
probe result / screenshot         absent
daemon process                    authenticated and retired
error                             Darwin process identity read was incomplete
evidence SHA-256                  efb002765f4d55cb39a25f74e31556fe683813f155f3e4fb3bf94167e0179ad3
```

The controller enumerated every system PID, fully captured its Darwin BSD
identity, and filtered by EUID only afterwards. One normal transient or
unreadable PID could therefore abort the same-EUID baseline. Because assignment
never completed, final cleanup also reported `pre-launch process baseline is
unavailable`. This is a controller proof-harness liveness defect; it is not
FreeCAD, plugin or daemon product evidence because `launch.count` is exactly
zero.

PID 54712, all relevant exact-name processes, sockets, receipts, grants,
checkouts and open handles were absent after the failure. The exact 76 KiB
isolated fixture root was moved without changing its inode into the protected
0700 evidence root as `failed_isolated_root_v01`; it was not deleted. The
original `/private/tmp/vc-g1v01-db604533` path is absent. The failed evidence,
controller, probe, bootstrap request/key, task records and model fixtures are
therefore recoverable and remain private.

#### Proposed MRG1-G1-C02-A07

A07 would authorize one successor acceptance attempt named V02. It does not
authorize a V01 retry and does not change any product, protocol or UI byte.

Before V02 may run, a separate protected evidence root and controller must:

1. enumerate only same-EUID PIDs with Darwin `PROC_UID_ONLY`;
2. record PID, stage, observed byte count and `errno` for incomplete identity
   reads;
3. use bounded recapture plus fresh UID censuses, skipping a PID only after it
   is proven absent and failing closed if it remains unreadable;
4. preserve two-snapshot generation stability, explicit daemon inclusion,
   pre-first-census escape detection, PID-reuse rejection and the
   no-ambiguous-signal rule; and
5. accept a stable owner-private empty daemon run root as retired, matching
   production cleanup semantics.

The successor controller must first produce deterministic RED/GREEN evidence,
pass its full mock/static/repository/runtime gates, receive a fresh sol-max
`0/0/0/0` review and an independent terra-medium preflight. A07 authorizes
exactly one V02 controller invocation after those conditions are met. V02 has
no retry; any observation defect is terminal. Staging, commit and push remain
forbidden until V02 succeeds or the user separately accepts a failed-runtime
closeout.

#### MRG1-S64

1. **Completed:** FIX05-FIX07 are GREEN; independent C02 217, C01 257+1,
   static gates and final `0/0/0/0` all passed.
2. **Consumed:** the only authorized V01 invocation is terminal FAIL before
   GUI launch; no V01 retry is permitted.
3. **Next:** user decision on proposed MRG1-G1-C02-A07. No successor
   controller or real process may start before explicit approval.
4. **Authority:** standing technical authority covered diagnosis, evidence
   preservation and exact cleanup. It does not broaden the exhausted
   exactly-one-V01 authorization.
5. **Discipline:** product exact-eight bytes remain frozen; index is empty;
   commit and push remain HOLD.

| Entry ID | Decision / approval | Commit / push | Gate evidence | Residual | Snapshot | State |
|---|---|---|---|---|---|---|
| MRG1-G1-C02-E41 | A04 C02; FIX05-FIX07 | `not-created`; forbidden | component 217; C01 257+1; static PASS; final review `0/0/0/0` | V01 real runtime | MRG1-S64 | automated GREEN / exact bytes frozen |
| MRG1-G1-C02-E42 | exhausted V01; A07 proposed, not approved | `not-created`; forbidden | controller exit 1; launch 0; daemon retired; evidence `efb002…`; isolated root preserved | successor V02 or failed-runtime closeout requires user decision | MRG1-S64 | terminal V01 FAIL / approval gate open |

### 97.41 MRG1-G1-C02-A07 authorization and V02 task contract

At `2026-07-29T10:55:47Z`, the user replied exactly:

```text
批准 MRG1-G1-C02-A07
```

The approval is bound to proposed A07 in Section 97.40 at artifact SHA-256
`8c957142a37475a1265e0195ae314c6f48f213141c1f26cf8f088c44a696fb9f`,
repository HEAD/upstream
`0e1d2b0db9523685dacbdb5261ac87a14c1dfbf6`, an empty index and the
FIX07 exact-eight hashes in S64. It authorizes the following exact contract
without changing or retrying V01.

#### Context

V01 is immutable terminal failure evidence. It proved only that its controller
could not construct a liveness-tolerant same-EUID Darwin process baseline;
FreeCAD GUI launch count was zero. V02 succeeds only if the unchanged FIX07
product bytes complete the frozen real FreeCAD/PySide acceptance probe and all
controller, daemon, GUI, checkout and process residue retires.

#### Decisions

- `MRG1-G1-C02-A07-D01`: create a separate 0700 V02 evidence root. The S64
  V01 root, controller, probe, failed evidence and preserved isolated fixture
  tree are read-only and out of the V02 allowlist.
- `MRG1-G1-C02-A07-D02`: V02 process baseline enumeration uses Darwin
  `PROC_UID_ONLY` for the exact effective UID.
- `MRG1-G1-C02-A07-D03`: an incomplete BSD identity read records PID, stage,
  observed bytes and `errno`; bounded fresh UID censuses may omit it only after
  proving that generation absent. A persistent unreadable PID is terminal.
- `MRG1-G1-C02-A07-D04`: two-snapshot stability, explicit daemon inclusion,
  PID generation reuse, PGID/SID escape detection and no ambiguous signal
  remain mandatory.
- `MRG1-G1-C02-A07-D05`: daemon retirement accepts only an absent run root or
  a stable, exact-owner, private, empty run root, matching production cleanup.
- `MRG1-G1-C02-A07-D06`: V02 is one controller invocation with zero retries.
  Any unexpected observation or cleanup defect is terminal.

#### Commit sequence

| ID | Commit | Scope | Gate |
|---|---|---|---|
| MRG1-G1-C02-CLOSE | `feat(workbench): preview managed head and draft` | artifact plus frozen C02 exact-eight only | V02 exit 0, screenshot/evidence inspection, exact stage, cached mechanical gate |

The external V02 controller/probe are acceptance evidence and are not
repository commit paths. No commit may be created before V02 succeeds.

#### Manual validation matrix

| ID | Environment | Scenario | Expected observation | Owner |
|---|---|---|---|---|
| V02-M01 | canonical managed FreeCAD 1.1.0 / Python 3.12 / PySide | open frozen HEAD and Draft, project both documents, review exact Draft | screenshot and probe evidence show two correct documents, exact task/base/candidate authority and one accepted review | controller plus root visual inspection |
| V02-M02 | same invocation cleanup | retire documents, checkouts, daemon, GUI and all authenticated process generations | zero socket/receipt/grant/checkout/process/run-root residue; unknown generation is fail-closed and never signaled | controller plus terra mechanical audit |

#### Budgets and circuit breakers

- One V02 evidence root, one successor controller, one successor probe and one
  real V02 invocation.
- Zero V01 or V02 retries and zero real FreeCAD/daemon launches during
  implementation, review or mechanical preflight.
- Any out-of-allowlist write, product hash drift, index change, unexpected
  RED, formatter drift, ambiguous process state, missing evidence field or
  cleanup residue freezes execution before V02 or makes V02 terminal.
- Long-running commands retain their original native session and are never
  duplicated.

#### File allowlist

Before the real gate, writes are limited to:

1. this controller-owned artifact;
2. a new protected external V02 root containing only the successor controller
   and probe until its single invocation; and
3. temporary mock/static logs outside the repository.

The FIX07 exact-eight product/test paths are frozen read-only. The complete V01
evidence root is frozen read-only. The excluded `.workbuddy/`,
`CAD_Theory_Course_Parametric_Learning.md` and
`CAD_Theory_Course_Scripts_V8_True3000.md` remain unread and unstaged.

After V02 succeeds, the only repository stage allowlist is this artifact plus
the FIX07 exact-eight paths. No broad add command is permitted.

#### Expected impact

The controller proof harness becomes tolerant of proven-vanished same-UID
process churn without weakening persistent unreadable, PID reuse or escape
fail-closed behavior. Product code, protocol, UI, managed runtime and fixture
semantics do not change. The expected automated product baselines remain C02
217 and C01 257 passed plus one deselected.

#### Residuals

| ID | Evidence | Impact | Disposition / closure |
|---|---|---|---|
| V02-RES-01 | immutable V01 evidence `efb002…`, launch count 0 | no successful real GUI evidence yet | closed only by V02 exit 0 or a separately approved failed-runtime closeout |
| V02-RES-02 | V01 census omitted failing PID/read details | reduced failure diagnosis | close through D03 structured evidence and mock assertions |
| V02-RES-03 | V01 daemon private empty run root was classified absent-only | false cleanup failure | close through D05 and exact empty/private/owner tests |

#### Capability profile and adapter

Permitted evidence-source categories:

- `live capability declarations`: `update_plan`, `spawn_agent`,
  `followup_task`, `send_message`, `wait_agent`, `exec_command`,
  `write_stdin` and `apply_patch` are declared live.
- `observable behavior`: native planning updates, agent spawn/follow-up/wait,
  controllable exec sessions and original-session polling have all completed
  successfully in this campaign.
- `environment identity`: Codex desktop is attached to
  `/Users/wangtao/Documents/DevProject/vibecad` on branch
  `codex/agent-stage3`.
- `public configuration`: filesystem access is unrestricted, network access
  is enabled and approval policy is `never`; none of these expand the approved
  A07 scope.

Selected adapter and profile:

```text
adapter: Codex
approval: native-plan
delegation: spawn-send-wait
persistence: repo-artifact
process: native-session-poll
```

User-specific model routing supersedes the adapter defaults: mechanical gates
use `gpt-5.6-terra / medium`, ordinary coding uses
`gpt-5.6-sol / high`, and architecture/adversarial review uses
`gpt-5.6-sol / max`.

#### MRG1-S65

1. **Completed milestones:** S64 and immutable V01 terminal evidence are
   preserved; A07 is explicitly approved; no commit exists; HEAD/upstream
   remain `0e1d2b0…`; index is empty.
2. **Next steps:** I91 builds the successor controller/probe with deterministic
   RED/GREEN; I92 performs sol-max closure review; I93 performs terra
   mechanical preflight; only exact GREEN admits the sole V02 invocation.
3. **Approved decisions:** MRG1-A04 and MRG1-G1-C02-A07, with the exact user
   authorization above; A07-D01 through A07-D06 are active.
4. **Execution discipline:** Codex `spawn-send-wait`,
   `native-session-poll`, external V02-only write allowlist, frozen product and
   V01 bytes, zero retries, stop on any circuit breaker.

| Entry ID | Decision / approval | Commit / push | Gate evidence | Residual | Snapshot | State |
|---|---|---|---|---|---|---|
| MRG1-G1-C02-E43 | A07 approved at artifact `8c957…`; A07-D01..D06 | `not-created`; forbidden | HEAD/upstream exact; index empty; V01 evidence frozen | V02-RES-01..03 open | MRG1-S65 | approved / I91 next |

### 97.42 I91-A setup circuit breaker

I91 created protected root
`/var/folders/qk/0_b6krc135j3lrz44krcddr40000gn/T/vibecad-v02-ZAKA93d4`
with only `v02_controller.py` and `v02_probe.py`, both mode 0600. V01
evidence and repository bytes remained unchanged.

The uncorrected seed produced the four predicted genuine failures:

1. UID census routed `(type=1, typeinfo=0)` instead of
   `(PROC_UID_ONLY=4, euid=501)`;
2. one transient incomplete BSD read terminated the baseline rather than
   invalidating and recapturing the whole snapshot;
3. persistent incomplete evidence lacked PID, stage, observed bytes and
   `errno`; and
4. a stable owner-private empty daemon run root was classified unretired.

The first implementation candidate then entered its single focused GREEN
command. It exited 1 in 0.13 seconds at heredoc line 190 before producing a
case count. Per circuit breaker, no semantic edit or broader gate followed.

One controller-authorized read-only diagnostic proved all nine run-root helper
cases matched their expected result: owner-0700 empty and absent passed;
symlink, non-owner, non-private, non-empty, replaced inode, second-scan entry
and replaced parent failed. The only integration failure was a test double
named `RunPath` that lacked `__truediv__`; production cleanup evaluates
`run_root / "daemon.sock"`. This is a mock setup error, not semantic product or
controller evidence. The diagnostic did not edit either V02 file.

Current candidate hashes at the breaker:

```text
v02_controller.py  faad087c51541361bf1c12b9e0da4a966d34c5eb9b3cec0d7aa1af3b81fed508
v02_probe.py       7200979459c8037ebce9db04286b84832342a2e36387dc220fb1bd1abb71a265
```

#### MRG1-S66

1. **Completed milestones:** A07/S65 is active; V02 namespace isolation and
   genuine RED evidence are complete; run-root helper matrix is 9/9.
2. **Next steps:** I91B may correct only the external mock `RunPath` setup and
   rerun the single focused matrix. Any semantic failure stops before broader
   gates; exact GREEN returns to I91's remaining gates.
3. **Approved decisions:** A07-D01..D06 remain unchanged; no additional user
   approval is required for the setup-only continuation.
4. **Execution discipline:** sol-high implementer, external V02-only writes,
   native-session process handling, no real process, V01/product freeze and
   all S65 circuit breakers remain active.

| Entry ID | Decision / approval | Commit / push | Gate evidence | Residual | Snapshot | State |
|---|---|---|---|---|---|---|
| MRG1-G1-C02-E44 | A07 I91-A | `not-created`; forbidden | four genuine REDs; first GREEN exit 1/0.13s; diagnostic run-root 9/9 | mock `RunPath` setup only | MRG1-S66 | gate RED / I91B setup continuation |

I91B corrected only the external `RunPath` test double. Its single focused
matrix then returned `27 passed / 0 failed`, exit 0 in 2.37 seconds. It covered
UID type/euid and positive stale `errno`, ten sizing/fill/duplicate cases,
whole-snapshot dirty retry, transient and persistent incomplete reads, capture
churn, bounded final failure, delta drift, fresh mismatch no-signal, all nine
run-root cases and retirement integration.

The following eight-hook semantic command did not import the controller or
execute a case. Its local heredoc used illegal Python syntax:
`lambda **kwargs, _variant=variant`. It exited 1 with `SyntaxError` in 1.16
seconds. V02 hashes remained `faad087…` and `720097…`; no real process ran.
I91C may change only that ephemeral setup to
`lambda _variant=variant, **kwargs` and invoke the semantic command once.

#### MRG1-S67

1. **Completed milestones:** focused I91B is 27/27 GREEN; V02 semantic bytes
   are unchanged from S66.
2. **Next steps:** I91C corrects one lambda setup and runs the eight semantic
   hooks once. Semantic RED freezes; exact GREEN resumes remaining I91 gates.
3. **Approved decisions:** A07-D01..D06 remain active without scope change.
4. **Execution discipline:** setup-only external mock correction, sol-high,
   no duplicate command, no real process, V01/product freeze.

| Entry ID | Decision / approval | Commit / push | Gate evidence | Residual | Snapshot | State |
|---|---|---|---|---|---|---|
| MRG1-G1-C02-E45 | A07 I91B | `not-created`; forbidden | focused 27/27; semantic command setup exit 1/1.16s, zero cases | ephemeral lambda syntax only | MRG1-S67 | focused GREEN / I91C setup continuation |

### 97.43 I91 implementation closure

I91C corrected only the ephemeral lambda order. The eight semantic hooks then
returned 8/8 GREEN in 2.18 seconds. Four errno cases and fourteen static
constraints were GREEN before formatting. Ruff format-check reported only six
mechanical folds; one authorized formatter invocation made no semantic change.
Post-format gates returned:

```text
focused UID/run-root matrix       27/27 in 2.22s
semantic S1-S8                    8/8
escape subset                     6/6
errno matrix                      4/4
static assertions                 13/13
py_compile / Ruff / format        PASS
repository exact-eight            8/8
runtime authentication            PASS
```

The first runtime harness called `_runtime_authentication` successfully, then
its own extra assertion read nonexistent key `expected["schema_version"]`.
It exited 1 with `KeyError` in 8.71 seconds. I91D removed only that
out-of-contract assertion. Its one exact helper gate returned PASS in 1.42
seconds, including canonical prefix, generation, receipt, FreeCAD aliases and
environment restoration.

The frozen implementer candidate is:

```text
root               /var/folders/qk/0_b6krc135j3lrz44krcddr40000gn/T/vibecad-v02-ZAKA93d4
root mode/files    0700; exactly v02_controller.py and v02_probe.py
controller         c42a63939a186cdae29c888973bba0275269eab28dc341d6ce46fed0a4086a96
controller size    103687; mode 0600
probe              7200979459c8037ebce9db04286b84832342a2e36387dc220fb1bd1abb71a265
probe size         46298; mode 0600
```

V02 has no `V01`/`v01` namespace text. It uses `v02_*`,
`VIBECAD_V02_*` and `vc-g1v02-*`. UID list sizing/fill rejects zero,
negative, misaligned, duplicate, saturated and over-bound results. Positive
results do not fail on stale `errno`; the value is retained for audit.
Every dirty capture invalidates the whole snapshot and clears the previous
adjacent observation. One global eight-attempt/0.5-second budget admits only
two adjacent complete identical token tuples, including exact daemon and
controller generations.

The run-root predicate opens the canonical parent, opens the leaf relative to
that parent with no-follow, requires exact owner/mode/type/identity, scans the
same directory FD empty twice and verifies stable parent and leaf identities.
It never deletes a run root. Unknown or changed process generations never gain
signal authority, and a fresh full-token mismatch prevents signaling.

The review claim is deliberately narrow:

- two adjacent complete identical observations are not historical continuous
  stability;
- Darwin `PROC_UID_ONLY=4` with EUID typeinfo is a locally evidenced private
  ABI assumption; and
- the final fresh token read to `kill(pid)` still has an irreducible
  generation-reuse interval without a kernel generation-bound signal handle.

These are inherited non-adversarial same-EUID environment assumptions, not
claims that the controller solved a kernel primitive it does not possess.

The V01 frozen hashes and four-entry evidence root, repository
HEAD/upstream/index, FIX07 exact-eight and all excluded paths remained
unchanged. No controller main, `_execute`, FreeCAD, daemon or V02 process ran.

#### MRG1-S68

1. **Completed milestones:** I91 genuine RED, implementation and implementer
   gates are complete; candidate hashes above are frozen.
2. **Next steps:** synchronize only the controller's expected artifact hash to
   this appended ledger revision, rerun the affected repository/static gate,
   then issue I92 to a fresh sol-max reviewer.
3. **Approved decisions:** A07-D01..D06 remain active; no real invocation has
   been consumed.
4. **Execution discipline:** V02 root is frozen except for one expected ledger
   hash synchronization or a reviewer-approved correction; final review and
   terra preflight remain independent; no real process.

| Entry ID | Decision / approval | Commit / push | Gate evidence | Residual | Snapshot | State |
|---|---|---|---|---|---|---|
| MRG1-G1-C02-E46 | A07 I91-I91D | `not-created`; forbidden | RED 4; focused 27; semantic 8; escape 6; errno 4; static 13; repo/runtime PASS | V02-RES-04 private ABI assumption; V02-RES-05 read-to-signal interval | MRG1-S68 | implementer GREEN / artifact-hash sync next |

### 97.44 I92 final-review NO-GO

I91E changed exactly one expected artifact-hash literal to S68 SHA-256
`721230525c32020e98b63e43ae2db767b070f23136cc7c77b5a4292d0881fb54`.
The resulting controller was
`259aee8a90e6ed16234df8c178508e81db043ea145193a5d0eb184d6adedcf38`.
Ruff, format, compile and repository exact-eight gates were GREEN.

Independent I92 sol-max review returned canonical
`0 Critical / 1 Major / 3 Moderate / 0 minor`, therefore HOLD:

1. **Major — unnamed partial isolated root.** `_make_isolated_root` may create
   `/private/tmp/vc-g1v02-*` and fail during child initialization before its
   caller assigns the returned tuple. Final evidence then records
   `isolated_root: null` while an unreported directory may remain.
2. **Moderate — deadline acceptance after expiry.** A second complete
   identical snapshot returns success before the 0.5-second global deadline is
   checked. A deterministic hook completed it at 0.6 seconds and observed
   acceptance.
3. **Moderate — structured identity evidence erased.**
   `_seen_generation_absent` and initial GUI token capture replace
   `_IncompleteProcessRead` with generic messages, losing PID, stage, observed
   bytes and `errno` although signaling remains fail-closed.
4. **Moderate — false `run_root_absent` field.** The empty-or-absent safety
   predicate is stored as `run_root_absent=true` even when a stable private
   empty directory exists. D05 permits that state, but evidence cannot
   distinguish it from actual absence.

I92 independently closed UID scope, sizing/fill, whole-snapshot reset,
full-token adjacency, daemon/self inclusion, generation delta, no-ambiguous
signal, final census, V02 namespace/O_EXCL and the run-root no-follow/double
observation predicate. Its pure hooks were 8/8 GREEN on those closed paths.

Required closure tests are:

- fail after root creation and after each child setup; evidence must name and
  preserve the exact root, with no unreported prefix directory;
- reject an equal second census completed after deadline and retain a
  pre-deadline control;
- preserve structured incomplete-read fields through `_same_generation`,
  GUI capture and durable evidence while proving zero signals; and
- record run-root clean state as actual `absent` or `existing_empty`, using
  that state rather than a mislabeled absence boolean.

No reviewer wrote a file or launched controller main, `_execute`, Popen,
FreeCAD, daemon or a signal. Repository, V01 and V02 hashes/cardinality remained
frozen.

#### MRG1-S69

1. **Completed milestones:** I91 implementer candidate and I92 independent
   review are complete; V02 has not run.
2. **Next steps:** I94 uses genuine RED for the four I92 findings, applies the
   smallest V02-controller-only correction and repeats affected plus full
   implementer gates. I95 is a fresh sol-max closure review.
3. **Approved decisions:** A07-D01..D06 remain active; the four findings are
   inside the approved proof-harness scope.
4. **Execution discipline:** only `v02_controller.py` may change; probe,
   product, artifact outside controller-owned appends and all V01 bytes are
   frozen; unexpected RED or real process freezes execution.

The S65 phrase “zero run-root residue” is clarified here: a stable
owner-private empty daemon run root is permitted by D05 and must be reported
as `existing_empty`; the outer V02 isolated fixture root still follows its
separate final cleanup/evidence policy.

| Entry ID | Decision / approval | Commit / push | Gate evidence | Residual | Snapshot | State |
|---|---|---|---|---|---|---|
| MRG1-G1-C02-E47 | A07 I92 | `not-created`; forbidden | review `0/1/3/0`; independent closed-path hooks 8/8 | four exact closure tests above | MRG1-S69 | NO-GO / I94 next |

### 97.45 External V01/V02 evidence-root loss and proposed A08 recovery

At `2026-07-29T23:17:09Z`, controller recovery found that both the frozen V01
evidence root and the uninvoked V02 implementation root were absent. The
repository remained at
`0e1d2b0db9523685dacbdb5261ac87a14c1dfbf6`, equal to its upstream, with an
empty index and the S64 exact-eight product/test hashes unchanged. A bounded
process sweep found no FreeCAD, daemon, V02 or `vc-g1v02` process. Therefore
the V02 real invocation count remains zero and no product or runtime action was
consumed.

The I94 implementer confirmed that it did not remove either evidence root. Its
only removals were self-owned empty per-case mock directories; the final I94
freeze verified the V02 root afterwards. Bounded read-only recovery searched
the system temporary roots, repository, Trash, open deleted files, Spotlight,
Python bytecode locations and available APFS/Time Machine snapshot metadata.
No complete controller or probe copy was found. The last observed I94 identity
was:

```text
controller size/hash  107023 / af9fb83e88dab0c95b060cb8a6cf9685a8e0a21e5dbd2820f9b05f2983f35512
probe size/hash        46298 / 7200979459c8037ebce9db04286b84832342a2e36387dc220fb1bd1abb71a265
root/files             0700 / exactly two 0600 regular files
artifact hash          a5faf2f412c5526bf586df178ff4347ed58d257bb13e7ad73a9f0a6d6fd88728
```

The preserved I94 transcript records the expected `10 fail / 2 control pass`
RED, corrected `12/12`, focused `27/27`, semantic `8/8`, escape `6/6`, errno
`4/4`, static `13/13`, compile/Ruff/format, repository and runtime-helper
GREEN. Those results describe lost bytes and cannot admit any reconstructed
controller or the real gate.

An independent fresh `gpt-5.6-sol / max` recovery audit concluded:

1. byte-exact restoration from a complete, provenance-bound copy would remain
   inside A07, but no such copy exists;
2. hashes and partial transcript patches cannot reconstruct the lost complete
   files or bind the old gates to new bytes;
3. S69 allowed only a minimal controller correction on a frozen candidate,
   kept the probe frozen and budgeted one evidence root/controller/probe; and
4. a new implementation identity therefore requires a new exact recovery
   authorization, full gates and full-file review before any real process.

The missing external evidence is an execution-proof breaker, not a product
failure. The V01 ledger facts remain historical records but their private
binary evidence root is no longer independently inspectable.

#### Proposed MRG1-G1-C02-A08

A08 would supersede A07 only for the lost external proof-harness identity. It
would not change C02 product, protocol, UI, exact-eight bytes, commit subject
or acceptance semantics.

A08 would authorize:

1. one new logical recovery attempt named `V02R`, distinct from never-invoked
   V02;
2. one durable owner-private evidence root at
   `/Users/wangtao/Documents/DevProject/vibecad-c02-evidence/MRG1-G1-C02-V02R`,
   with root and created parents mode 0700;
3. reconstruction and freezing of exactly one `v02r_controller.py` and one
   `v02r_probe.py`, both 0600, from the approved A07-D02..D05 contract and
   current frozen product surface rather than from an unverifiable claim of
   byte equivalence;
4. fresh genuine RED and control evidence for A07-D02..D05 and all four I92
   findings, followed by the complete focused, semantic, escape, errno,
   static, compile, Ruff/format, repository exact-eight and runtime
   authentication gates;
5. a fresh `gpt-5.6-sol / max` full-file adversarial review and an independent
   `gpt-5.6-terra / medium` mechanical preflight on the final exact bytes; and
6. only after every gate is GREEN, exactly one real V02R controller invocation
   with zero retries, followed by root visual inspection of the screenshot and
   evidence.

The pre-run root must contain exactly the two frozen implementation files.
After the sole admitted invocation it may additionally contain only the
controller-declared evidence hierarchy. It must not be deleted or relocated
before C02 commit, push and recovery closeout. V01, lost V02 identities,
product exact-eight, repository index and excluded user paths remain frozen.
Any unexpected RED, hash/cardinality/permission drift, out-of-scope write,
ambiguous process state, real launch before admission or incomplete cleanup is
terminal.

The approval phrase is:

```text
批准 MRG1-G1-C02-A08
```

#### MRG1-S70

1. **Completed milestones:** I94 genuine RED/correction and historical gates
   are recorded, but their external bytes are lost and their results are not
   reusable. HEAD/upstream and all C02 product hashes remain exact; V02
   invocation count is zero.
2. **Next steps:** wait for A08. If approved, append the exact authorization,
   create only the declared durable V02R root and run a new implementation and
   admission sequence from RED. Without approval, no controller/probe
   reconstruction or real process is permitted.
3. **Approved decisions:** A04 remains active for C02 product. A07-D02..D05
   remain the proposed semantic basis, but A07's lost evidence identity cannot
   admit execution. Proposed A08 is absent.
4. **Execution discipline:** Codex adapter with `native-plan`,
   `spawn-send-wait`, `repo-artifact` and `native-session-poll`; product and
   index frozen; exact named writes only; no retry or real process.

Permitted capability evidence remains:

- `live capability declarations`: `update_plan`, collaboration
  spawn/follow-up/message/wait, `exec_command`, `write_stdin` and
  `apply_patch` are declared live;
- `observable behavior`: native plan projection, agent recovery/audit
  delegation and bounded synchronous read-only commands completed in this
  recovery;
- `environment identity`: Codex desktop is attached to the declared
  repository and branch;
- `public configuration`: unrestricted filesystem and enabled network access
  are declared, but do not expand A04/A07 or authorize proposed A08.

| Entry ID | Decision / approval | Commit / push | Gate evidence | Residual | Snapshot | State |
|---|---|---|---|---|---|---|
| MRG1-G1-C02-E48 | A07; A08 proposed and absent | `not-created`; forbidden | product hashes exact; process 0; recovery audit requires new identity | V01/V02 external bytes lost; old I94 gates non-admitting | MRG1-S70 | blocked / approval gate open |

### 97.46 MRG1-G1-C02-A08 authorization and durable-root selection

At `2026-07-30T02:30:40Z`, the user replied exactly:

```text
批准 MRG1-G1-C02-A08  这种目录最好建在外置硬盘上
```

The approval is bound to proposed A08 in Section 97.45 at artifact SHA-256
`68819807f96baa87c4fd5b80dbf12f537a52769ba6f473bca16dd3f8930480e0`,
repository HEAD/upstream
`0e1d2b0db9523685dacbdb5261ac87a14c1dfbf6`, an empty index, the S64
exact-eight product/test hashes and zero V02/V02R real invocation count.

The controller then passively enumerated mounted volumes. No external physical
disk was present. `/Volumes/BOOTCAMP` reports an internal PCI-Express NTFS
volume and cannot be assumed to provide the required owner/mode semantics;
`/Volumes/Macintosh HD` is the system APFS volume. Neither is selected as an
external evidence target.

The user's external-drive preference is active, but is non-blocking because
the exact A08 proposal already approved a durable, non-temporary local root.
The active V02R root remains:

```text
/Users/wangtao/Documents/DevProject/vibecad-c02-evidence/MRG1-G1-C02-V02R
```

It is outside the repository and outside system temporary storage. The parent
and root must be owner-private mode 0700, and the two implementation files must
be 0600. The active root cannot be moved or replaced before C02 commit, push
and recovery closeout. If a qualifying external disk is later mounted, a
separate exact-path, hash-verified archival copy may be proposed or created
after closeout under the user's standing preference; that archive cannot
substitute for or mutate the active acceptance identity.

Active decisions are:

- `MRG1-G1-C02-A08-D01`: create one new logical recovery attempt named V02R;
- `MRG1-G1-C02-A08-D02`: use only the exact durable active root above;
- `MRG1-G1-C02-A08-D03`: reconstruct and freeze exactly one controller and
  one probe from the approved contract, with no claim of equivalence to lost
  bytes;
- `MRG1-G1-C02-A08-D04`: obtain fresh genuine RED/controls and every complete
  implementer gate on the reconstructed bytes;
- `MRG1-G1-C02-A08-D05`: require fresh sol-max full-file adversarial review
  and terra-medium mechanical preflight;
- `MRG1-G1-C02-A08-D06`: admit exactly one real V02R invocation only after all
  gates are GREEN, with zero retries; and
- `MRG1-G1-C02-A08-D07`: prefer a later hash-verified external-drive archive
  while refusing the currently observed internal NTFS volume as evidence
  storage.

No product, protocol, UI, repository source/test byte, commit subject or
acceptance behavior is changed by A08.

#### Current capability profile

Permitted evidence-source categories:

- `live capability declarations`: `update_plan`, `spawn_agent`,
  `followup_task`, `send_message`, `wait_agent`, `exec_command`,
  `write_stdin` and `apply_patch` are declared live;
- `observable behavior`: native plan projection, agent spawn/message/wait and
  bounded synchronous read-only commands have completed in this session;
- `environment identity`: Codex desktop is attached to
  `/Users/wangtao/Documents/DevProject/vibecad` on
  `codex/agent-stage3`;
- `public configuration`: filesystem access is unrestricted, network access
  is enabled and approval policy is `never`; these do not expand A08.

Selected adapter and profile:

```text
adapter: Codex
approval: native-plan
delegation: spawn-send-wait
persistence: repo-artifact
process: native-session-poll
```

User-specific routing remains: ordinary implementation uses
`gpt-5.6-sol / high`, full-file architecture/adversarial review uses
`gpt-5.6-sol / max`, and pure mechanical verification uses
`gpt-5.6-terra / medium`.

#### MRG1-S71

1. **Completed milestones:** S70 blocker is approved for recovery through
   A08; external-volume discovery selected the exact durable local root rather
   than internal NTFS; product hashes, HEAD/upstream and zero real-invocation
   count remain unchanged.
2. **Next steps:** create the exact 0700 parent/root, issue I96 sol-high to
   reconstruct the two 0600 files RED-first, freeze hashes, then issue
   independent I97 sol-max full-file review and I98 terra-medium preflight.
   Only exact GREEN admits the one V02R invocation.
3. **Approved decisions:** A04, A08-D01..D07 and the exact user authorization
   above are active. No repeat approval is required inside this boundary.
4. **Execution discipline:** Codex `spawn-send-wait`,
   `native-session-poll`, artifact plus exact external-root write allowlist,
   frozen product/index, no V01/V02 reconstruction, no real process before
   admission and stop on every Section 97.45 circuit breaker.

| Entry ID | Decision / approval | Commit / push | Gate evidence | Residual | Snapshot | State |
|---|---|---|---|---|---|---|
| MRG1-G1-C02-E49 | A08 approved at artifact `688198…`; A08-D01..D07 | `not-created`; forbidden before V02R | mounted-volume discovery: no external physical disk; HEAD/upstream/index/product exact | external archival target deferred until a qualifying disk is mounted | MRG1-S71 | approved / I96 next |

### 97.47 I96 V02R reconstruction and implementer GREEN

I96 reconstructed the A08 V02R controller and probe without claiming
equivalence to the lost V01/V02 bytes. No repository path, excluded path,
controller main, `_execute`, `Popen`, daemon, FreeCAD, GUI or signal was
touched.

The first pure RED command executed zero cases because its ephemeral
`importlib` loader had not registered the module in `sys.modules` before
dataclass evaluation. The controller authorized only that setup correction.
The resulting genuine RED returned the exact predicted result:

```text
controls  C1 artifact binding, C2 structured mapping       2 PASS
R1..R10   UID census, recapture, structured evidence,
          GUI identity, deadline, run-root truth and
          isolated-root publication/prefix closure         10 FAIL
```

The RED harness exited normally after reporting `pass=2 fail=10`; it did not
exercise a real process. I96 then implemented the complete two-file candidate.
Setup-only corrections during the implementer gates were limited to canonical
`/private/var` resolution, correct control/focused count separation,
out-of-root compile outputs and exact pycache cleanup, the documented
`product_hashes` runtime-auth key, current product-path mapping, a supported
macOS `ps` field list and one bounded Ruff formatting pass. Every affected
gate was rerun on the final bytes.

Final implementer evidence:

```text
controls / focused / semantic     2 / 27 / 8
escape / errno / static           6 / 4 / 15
py_compile / Ruff / format        PASS / PASS / PASS
runtime authentication            PASS
exact-eight product hashes        8/8
HEAD / upstream                   0e1d2b0… / 0e1d2b0…
index / relevant real processes   empty / 0
V02R invocation count             0
```

The runtime helper authenticated the canonical managed prefix, current
receipt and generation, Python/FreeCAD entry and target identities, artifact
hash and all eight frozen product hashes. Its observed FreeCAD and FreeCADCmd
SHA-256 values were respectively
`6c273f1c760ad9225a0a3f216b7ac03bcd2660ea2c3af4ae1c69ad62770bb061`
and
`47d8b5f7070f4327ced8a9684f0b4b8efb95a7142dc0b5c9cc0fa87e1ddf652a`.

Frozen pre-review identity:

```text
root               /Users/wangtao/Documents/DevProject/vibecad-c02-evidence/MRG1-G1-C02-V02R
parent/root         owner uid 501; mode 0700
root cardinality    exactly two regular files
controller          1633aa65c35ff312e97dec0b28a266d4b46d7de83bdfbab1b4649c8a282fa1a8
controller size     52771; mode 0600; nlink 1
probe               6dbcc66f78454ccfe202d6b4eeca6deff960902e5b43bb51f465d0f076baf2e5
probe size          29312; mode 0600; nlink 1
artifact literal    78652711c927a127d1007e43e74bdd5563337436189348ea462954b8eb554554
```

The controller implements same-EUID Darwin census, structured incomplete-read
evidence, bounded whole-snapshot adjacency and deadline, full process and
executable generation tokens, exact-PID signal authority, run-root
`absent|existing_empty` proof, root publication before child initialization,
exclusive durable evidence, one-shot consumption before the sole Popen,
runtime/product/module/document/review/screenshot binding and acknowledged
document -> checkout -> client/worker -> daemon cleanup.

Residual platform assumptions remain recorded rather than waived:

- Darwin `libproc` and `PROC_UID_ONLY=4` are platform-specific;
- the final fresh identity read and exact-PID signal are not one kernel-atomic
  primitive;
- adjacent equal censuses prove bounded, not historical, stability;
- managed Python's resolved target is mode 0775 but is receipt/generation/hash
  bound and not world-writable; and
- no qualifying external disk is mounted, so the approved durable local root
  remains the active identity.

This append changes the artifact hash. Before independent review, the
controller may change exactly its one expected artifact-hash literal to this
Section 97.47 revision, followed by artifact-binding, compile, Ruff, format,
root-cardinality, product-hash, repository and process-zero checks. No other
byte may change.

#### MRG1-S72

1. **Completed milestones:** A08 I96 genuine RED and full implementer GREEN
   are complete; the exact two-file pre-sync identity above is frozen; real
   invocation count is zero.
2. **Next steps:** perform the one-literal artifact-hash sync, then run I97
   sol-max full-file adversarial review and I98 terra-medium mechanical
   preflight concurrently on the same exact hashes. Both GREEN admit the sole
   V02R invocation; either finding freezes it.
3. **Approved decisions:** A04 and A08-D01..D07 remain active; the one literal
   sync and concurrent read-only gates are inside A08-D04/D05.
4. **Execution discipline:** exact external controller-only sync, then
   immutable/read-only parallel review; product/index frozen; no artifact
   append between final sync and the real invocation; no real process before
   dual GREEN.

| Entry ID | Decision / approval | Commit / push | Gate evidence | Residual | Snapshot | State |
|---|---|---|---|---|---|---|
| MRG1-G1-C02-E50 | A08 I96 | `not-created`; forbidden | genuine RED 2/10; GREEN 2/27/8/6/4/15; compile/Ruff/format/runtime/repo PASS | five recorded platform/storage assumptions; independent gates open | MRG1-S72 | implementer GREEN / one-literal sync next |

### 97.48 Parallel I97/I98 gates and I97 NO-GO

After the authorized one-literal sync, the exact parallel-gate identity was:

```text
artifact    6e65d373cc2e509a36d3fe8a1b20328c8860d200dc5d669283b612386dee5af9
controller  4016662604b9383e4501ccbf0478c2cd350a38dbb391c1ddd4c31f48419ba10c
probe       6dbcc66f78454ccfe202d6b4eeca6deff960902e5b43bb51f465d0f076baf2e5
```

I97 sol-max full-file adversarial review and I98 terra-medium mechanical
preflight ran concurrently against those same immutable hashes. I98 returned
PASS: exact hashes, sizes, uid/modes/nlinks/cardinality, in-memory compile,
Ruff, format, fifteen static constraints, runtime authentication, all eight
product hashes, HEAD/upstream/index/diff and process/residue checks were
GREEN. Its first runtime-helper call lacked `VIBECAD_FREECAD_ENV` and rejected
as designed; the explicitly injected canonical managed prefix then passed.

I97 returned `0 Critical / 3 Major / 2 Moderate / 0 minor`, therefore NO-GO:

1. **Major — PID reuse omitted from residue.** Final residue compared only
   PID keys rather than full generation tokens, so baseline `P/A` followed by
   final `P/B` could be omitted.
2. **Major — root registration still late.** After `mkdir`, `chmod` and
   private-directory admission could fail before durable registration, and
   `_make_isolated_root` was called before the outer preservation `try`.
3. **Major — GUI proof chain under-bound.** The controller did not bind the
   captured GUI executable to the authenticated FreeCAD identity, deeply
   validate module/product/bootstrap identities, or strictly read and compare
   durable `probe-result.json` with stdout.
4. **Moderate — opened run-root not re-admitted.** The leaf FD identities were
   not compared to the first path identity and owner/type/0700 were not
   revalidated after open.
5. **Moderate — RPC thread/exact-once evidence discarded.** Probe operation
   thread IDs were captured but omitted from binding evidence, and controller
   validation did not require worker-only RPCs or
   `request_count == effect_count == 1`.

Four pure hooks reproduced generation-reuse omission, under-bound result
acceptance, an unregistered post-mkdir root and post-open mode substitution.
No real controller, Popen, daemon, FreeCAD, GUI, network or signal ran.

I97's hook import omitted `PYTHONDONTWRITEBYTECODE=1` and created exactly
`__pycache__/v02r_controller.cpython-314.pyc` inside the active root. The
reviewer stopped and left it observable. The controller verified the precise
file identity and hash, removed only that generated file, then removed only
its empty `__pycache__` directory. Root cardinality returned to exact two and
the controller/probe hashes above remained unchanged.

This result confirms that read-only review and mechanical gates can run in
parallel, but dynamic hooks must import only a private shadow copy under a
unique temporary gate root. Future reviewer packets may stat/hash/read the
active root but cannot import it. Repo test gates use independent worktrees or
temp/cache roots; the sole real FreeCAD gate remains serial because worktrees
do not isolate the macOS process table, runtime, sockets or one-shot identity.

#### MRG1-S73

1. **Completed milestones:** I98 is independent PASS; I97 is canonical NO-GO
   with five reproduced findings; exact generated-byte cleanup restored the
   active root; real invocation remains zero.
2. **Next steps:** I99 sol-high obtains genuine RED for all five findings,
   applies the smallest controller/probe corrections and reruns affected plus
   full implementer gates. After a final artifact-hash sync, fresh I100
   sol-max and I101 terra-medium run concurrently using isolated shadow-copy
   hook environments.
3. **Approved decisions:** A04 and A08-D01..D07 cover these proof-harness
   corrections; no product or acceptance scope changes and no repeated user
   approval is required.
4. **Execution discipline:** controller/probe-only writes, product/index
   frozen, `sol/high` implementation, isolated dynamic hooks, no active-root
   imports by reviewers, no real process before fresh dual GREEN.

| Entry ID | Decision / approval | Commit / push | Gate evidence | Residual | Snapshot | State |
|---|---|---|---|---|---|---|
| MRG1-G1-C02-E51 | A08 I97/I98 | `not-created`; forbidden | I98 PASS; I97 NO-GO `0/3/2/0`; hooks 4/4; exact pycache cleanup | five I97 findings open; real V02R still blocked | MRG1-S73 | correction required / I99 next |

### 97.49 I99 five-finding correction GREEN

I99 used a private owner-0700 shadow root and imported only copied bytes with
`/usr/local/bin/python3 -B`, `PYTHONDONTWRITEBYTECODE=1` and a shadow-local
pycache prefix. The active V02R root was never dynamically imported.

The first shadow command used unavailable `python` and exited 127 before any
case. The controller authorized only the verified absolute interpreter. The
fresh genuine RED then returned exactly:

```text
controls PASS                                     2
F1 generation residue                            1 FAIL
F2 root registration/preservation                2 FAIL
F3 GUI/module/durable-result binding              3 FAIL
F4 opened run-root re-admission                   1 FAIL
F5 RPC thread/exact-once evidence                 1 FAIL
```

I99 implemented only the five I97 closures. It added a pure production
generation-residue helper, made root-carrying initialization failure and
durable publication cover every post-mkdir failure, deeply bound GUI
executable/module/product/document/result identities, compared strict durable
probe result with stdout, re-admitted the opened run-root identity and
preserved/validated worker RPC threads, order and exact-once review evidence.

Two targeted GREEN attempts stopped on ephemeral assertion sequencing rather
than source behavior: the harness first asserted old exception classes and
then supplied an empty fixture that triggered an earlier strict rejection.
The controller authorized only the exact harness corrections. The final
targeted result was `10/10`; after one bounded Ruff exception-chain correction
and formatting byte change it was rerun and remained `10/10`.

Final implementer evidence:

```text
controls / focused / semantic     2 / 27 / 8
escape / errno / static           6 / 4 / 15
total pure gates                   62/62
compile / Ruff / format            PASS / PASS / PASS
runtime authentication             PASS
exact-eight / HEAD/upstream        8/8 / 0e1d2b0… equal
index / relevant processes         empty / 0
real invocation                    0
```

The final pre-ledger candidate was:

```text
controller  1769bcc33df7e3cb3bb83cf9ac999db3adf77eab0000537b70d1a716ceb5a8b8
size/mode   69414 / 0600 / nlink 1
probe       c554041f86772a76403d4a0536137f40d87d4e3b17ad99ea1275ae2dc8b9a6f8
size/mode   30523 / 0600 / nlink 1
root        uid 501 / 0700 / exact two
literal     4f7daf31487b3edb786b10905bc069ee59eff7f8c48c1e1973bc9749dfcdbec7
```

The shadow emitted no pycache, was removed by exact named paths, and no main,
`_execute`, Popen, daemon, FreeCAD, GUI, network or signal ran.

This append changes the artifact hash. One final controller-only literal sync
to the Section 97.49 artifact revision is allowed, followed by binding,
compile, Ruff/format, hashes/cardinality/repository/process checks. Then fresh
I100 sol-max and I101 terra-medium run concurrently. Any dynamic hook must
import only a private shadow copy; the active root remains text/hash/stat-only.

#### MRG1-S74

1. **Completed milestones:** I99 genuine RED, five-finding correction,
   targeted `10/10` and full `62/62` implementer gates are GREEN; active root
   is exact-two and real invocation remains zero.
2. **Next steps:** final one-literal sync; concurrent isolated I100 full-file
   review and I101 mechanical gate. Dual GREEN admits the sole V02R run; any
   finding returns to correction without consuming it.
3. **Approved decisions:** A04 and A08-D01..D07 cover the correction and final
   parallel gates; no product change or new approval is required.
4. **Execution discipline:** immutable exact hashes after sync, shadow-only
   dynamic imports, `sol/max` plus `terra/medium` concurrent, no artifact
   append or other agent during the real run.

| Entry ID | Decision / approval | Commit / push | Gate evidence | Residual | Snapshot | State |
|---|---|---|---|---|---|---|
| MRG1-G1-C02-E52 | A08 I99 | `not-created`; forbidden | RED 2/8; targeted 10/10 twice; full 62/62; runtime/repo PASS | platform assumptions only; fresh dual gate open | MRG1-S74 | correction GREEN / final sync next |

### 97.50 I100 NO-GO and I101B corrected mechanical GREEN

Fresh I100 sol-max reviewed the complete frozen controller/probe identity:

```text
artifact    bcdecf50e258324cf8b7373758815c40154dcb61343b0835657aabca6ed02b27
controller  fba57a0da6133075917690e901de3613d01fd69401fcd3bb4025355f1acb96ff
probe       c554041f86772a76403d4a0536137f40d87d4e3b17ad99ea1275ae2dc8b9a6f8
```

Its canonical result was `0 Critical / 0 Major / 3 Moderate / 0 minor`,
therefore NO-GO:

1. **RPC request object identity remained under-validated.** The schema
   carried open-key, source, grant, checkout and review-request object IDs,
   but the controller validated only each response object ID. A private-shadow
   hook reproduced acceptance of all 24 malformed variants, including zero,
   negative, string and null request-side IDs.
2. **A narrower registered-root preservation gap remained.** Root creation,
   immediate registration and root-carrying initialization failures were
   closed, but post-return `lstat` and admission publication still ran before
   the main preservation `try`. Failure there left an exactly registered root
   outside the final evidence hierarchy.
3. **Failure residue detail was erased.** Full-generation residue detection
   was correct, but its exception path reset the residue mapping to empty
   before final evidence serialization, losing the offending generations.

I100's shadow tests created no pycache, were removed by exact named paths and
did not invoke main, `_execute`, Popen, daemon, FreeCAD, GUI, network or
signal. The active root, product files, repository index and relevant process
set did not drift.

The first I101 mechanical gate had reported a runtime receipt/generation
mismatch. Read-only diagnosis showed the repository runtime receipt and
generation were equal; the gate had imported a different installed
`vibecad.runtime` because it omitted the repository `PYTHONPATH`. Fresh I101B
terra-medium fixed only its gate environment:

```text
interpreter  /Users/wangtao/Documents/DevProject/vibecad/.venv/bin/python -B
PYTHONPATH   /Users/wangtao/Documents/DevProject/vibecad/src
runtime      paths.py / spec.py / status.py all from repository src
```

I101B was GREEN for exact hashes, owner/mode/nlink/cardinality, in-memory
compile, Ruff, format, fifteen static checks, receipt/generation and
Python/FreeCAD/FreeCADCmd identities, exact-eight, HEAD/upstream/index and
process-zero checks. The authenticated FreeCAD, FreeCADCmd and managed Python
SHA-256 values were respectively
`6c273f1c760ad9225a0a3f216b7ac03bcd2660ea2c3af4ae1c69ad62770bb061`,
`47d8b5f7070f4327ced8a9684f0b4b8efb95a7142dc0b5c9cc0fa87e1ddf652a`
and
`9edde9821d94b1875b6b0f62575d44b986fe01d5678f9209a6fc5a8cb6ece025`.
It imported only a private shadow copy, removed it exactly and made no real
invocation.

#### MRG1-S75

1. **Completed milestones:** I100 is canonical NO-GO with three reproduced
   Moderate findings; I101's environment false-red is resolved and I101B is
   mechanical GREEN; real invocation remains zero.
2. **Next steps:** I102 sol-high obtains genuine RED for the three findings,
   applies the smallest controller-only correction and reruns affected plus
   full implementer gates. After one final artifact-literal sync, fresh
   sol-max and terra-medium gates run concurrently on the new exact identity.
3. **Approved decisions:** A04 and A08-D01..D07 cover these proof-controller
   corrections; product behavior and UI remain frozen and no additional user
   approval is required.
4. **Execution discipline:** controller-only correction unless RED proves the
   probe must change; shadow-only dynamic imports; repository product/index
   frozen; no real process before fresh dual GREEN.

| Entry ID | Decision / approval | Commit / push | Gate evidence | Residual | Snapshot | State |
|---|---|---|---|---|---|---|
| MRG1-G1-C02-E53 | A08 I100/I101B | `not-created`; forbidden | I100 NO-GO `0/0/3/0`; I101B corrected mechanical PASS | three I100 Moderates open; real V02R still blocked | MRG1-S75 | correction required / I102 next |

### 97.51 I102 three-finding correction GREEN

I102 first synchronized only the controller's expected artifact literal from
the pre-S75 identity to the exact Section 97.50 artifact:

```text
pre-sync controller       fba57a0da6133075917690e901de3613d01fd69401fcd3bb4025355f1acb96ff
synced vulnerable         0610be8f6cf50e0f92f8a9db62f572e19d14b1c1c4222e58ec188ccd8b058703
artifact                  137011700e7f85ff9ec31d7dfad154c2d48ef9c455952f9fb9684915945e8ef4
```

A private-shadow genuine RED then returned `pass=4 fail=5`. Artifact binding,
valid RPC identity, registered-root admission and zero-residue controls all
passed. The five predicted failures were a boolean request object ID, a
negative request object ID, post-return root `lstat`, admission publication
and residue-map erasure. The only setup correction canonicalized macOS
`/tmp` to `/private/tmp`; the candidate was not changed before the formal RED.

I102 made three controller-only corrections:

1. every request-side RPC object ID is now exact `int > 0`, with booleans,
   invalid scalar types, per-operation duplicates and request/response aliases
   rejected;
2. a root-carrying admission-publication helper and the outer failure/final
   evidence flow now cover post-return `lstat`, publication and child
   environment setup, preserving the exact registered root; and
3. a strict structured process-residue failure retains the complete offending
   generation mapping in both final residue and structured error evidence.

The same-case GREEN was `9/9`. The expanded targeted matrix was `50/50`,
covering five request ID classes against zero, negative, string, null and
boolean inputs, alias/uniqueness cases, both registered-root failure windows,
and residue mapping, canonical serialization and invalid-type rejection.

Final implementer gates on the formatted bytes were:

```text
controls / focused / semantic     2 / 27 / 8
escape / errno / static           6 / 4 / 15
total pure gates                   62/62
expanded targeted                 50/50
in-memory and out-of-root compile PASS
Ruff check / format-check          PASS / PASS
runtime authentication             PASS
exact-eight                        8/8
HEAD / upstream / index            expected / equal / empty
related process / temp prefix      0 / 0
real invocation                    0
```

Runtime authentication explicitly loaded `paths.py`, `spec.py` and
`status.py` from repository `src` and bound the canonical managed prefix,
artifact, product hashes, receipt/generation and current FreeCAD,
FreeCADCmd and managed Python identities. One bounded Ruff format changed
bytes; all affected, targeted and full gates were rerun afterward. A targeted
expected count was corrected from 49 to the actual 50 without changing source
or assertions.

Frozen pre-ledger identity:

```text
controller  c84a856da5d7b753bdee0fa7ad1920fd73ec8d5ff994393189fd109adada21fb
size/mode   74399 / 0600 / nlink 1
probe       c554041f86772a76403d4a0536137f40d87d4e3b17ad99ea1275ae2dc8b9a6f8
size/mode   30523 / 0600 / nlink 1
artifact    137011700e7f85ff9ec31d7dfad154c2d48ef9c455952f9fb9684915945e8ef4
root        uid 501 / 0700 / exact two
```

All shadow files were removed by exact named unlink/rmdir operations. The
active root has no pycache, the probe and exact-eight were unchanged, and no
main, `_execute`, Popen, daemon, FreeCAD, GUI, network or signal operation
ran.

This append changes the artifact hash. Exactly one controller-literal sync to
this Section 97.51 identity is allowed, followed by binding, compile,
Ruff/format, root/cardinality, product, repository and process-zero checks.
After that sync the artifact and both evidence files remain immutable through
fresh parallel I103 sol-max full-file review and I104 terra-medium mechanical
preflight and, only on dual GREEN, the sole real V02R invocation.

#### MRG1-S76

1. **Completed milestones:** I102 genuine RED, three controller-only
   corrections, targeted `50/50` and full `62/62` gates are GREEN; probe and
   product are frozen; real invocation remains zero.
2. **Next steps:** perform the final one-literal sync, run fresh I103 sol-max
   and I104 terra-medium concurrently, then admit or block the unique real
   FreeCAD gate from their canonical results.
3. **Approved decisions:** A04 and A08-D01..D07 cover the final sync,
   independent gates and unique real invocation; no product/UI change or new
   approval is required.
4. **Execution discipline:** immutable exact identity after sync,
   shadow-only dynamic imports during review, explicit repository
   `PYTHONPATH`, no artifact append or agent mutation during the real run, and
   zero retries after invocation consumption.

| Entry ID | Decision / approval | Commit / push | Gate evidence | Residual | Snapshot | State |
|---|---|---|---|---|---|---|
| MRG1-G1-C02-E54 | A08 I102 | `not-created`; forbidden | RED 4/5; same-case 9/9; targeted 50/50; full 62/62; runtime/repo PASS | fresh dual gate and unique real invocation open | MRG1-S76 | correction GREEN / final sync next |

### 97.52 I103 cross-operation alias NO-GO and I104 PASS

The final parallel gates used the immutable identity:

```text
artifact    283e1be454505eba291daa597da0a5d4dedb3d5f934cde8e1e2a745ad82216fa
controller  76f97d7fe64a323958239a595d230357ad03082b822fa54ef466385ecd7834d2
probe       c554041f86772a76403d4a0536137f40d87d4e3b17ad99ea1275ae2dc8b9a6f8
```

I104 terra-medium returned final PASS. It independently reproduced the I102
same-case `9/9`, expanded `50/50`, full
`2/27/8/6/4/15 = 62/62` and static `15/15` gates. In-memory and out-of-root
compile, Ruff, format, scoped diff, explicit repository-source runtime
authentication, exact-eight, HEAD/upstream/index, evidence-root identity and
process/prefix-zero checks were all GREEN. The repository and active root did
not change, its shadow was removed exactly and real invocation remained zero.

I103 sol-max completed the full-file read and verified that the three I100
findings were individually closed. It then found one new Moderate: RPC object
identity uniqueness was enforced separately inside the `opened`, `claimed`,
`reviews` and `closed` operation buckets, but not globally across operations.
Consequently an `opened` request object ID could alias a `claimed`, `reviews`
or `closed` request object ID, or an ID from another bucket could alias a
response outside its own bucket, while all per-operation checks passed.

An independent I103B sol-max shadow hook confirmed the finding. Its valid
seven-operation RPC fixture set
`opened[0].open_key_object_id == claimed[0].grant_id_object_id == 100`;
every schema, count, order, thread and positivity constraint and every
per-bucket uniqueness/disjoint check was otherwise valid.
`_validate_rpc` returned normally:

```text
CROSS_OPERATION_ALIAS_ACCEPTED=1
GLOBAL_UNIQUE=False
```

I103B's first `/usr/bin/python3` loader was too old for dataclass `slots` and
failed before the hook. The only setup correction selected
`/usr/local/bin/python3` 3.14.2; the fresh run then reproduced the finding.
Both reviews used private shadow copies and did not call main, `_execute`,
Popen, daemon, FreeCAD, GUI, network or signal.

The canonical gate result is therefore
`0 Critical / 0 Major / 1 Moderate / 0 minor`, NO-GO. The smallest correction
is controller-only: flatten all five request-ID classes globally, require
global uniqueness and require that complete request set to be disjoint from
the complete response-ID set. Probe, product behavior and UI remain frozen.

#### MRG1-S77

1. **Completed milestones:** I104 is final mechanical PASS; I103/I103B
   independently confirmed one cross-operation alias Moderate; real
   invocation remains zero.
2. **Next steps:** I105 sol-high obtains a genuine cross-bucket alias RED,
   applies the controller-only global uniqueness/disjoint correction and
   reruns affected plus full gates. A final immutable identity then receives
   fresh parallel sol-max and terra-medium admission.
3. **Approved decisions:** A04 and A08-D01..D07 cover this evidence-validator
   correction; it changes no product feature, UI or acceptance shape and
   needs no additional user approval.
4. **Execution discipline:** controller-only mutation, shadow-only hooks,
   probe/product/index frozen, no real process before fresh dual GREEN.

| Entry ID | Decision / approval | Commit / push | Gate evidence | Residual | Snapshot | State |
|---|---|---|---|---|---|---|
| MRG1-G1-C02-E55 | A08 I103/I104 | `not-created`; forbidden | I104 PASS; I103/I103B NO-GO `0/0/1/0`; cross-bucket alias reproduced | one Moderate open; real V02R still blocked | MRG1-S77 | controller correction required / I105 next |

### 97.53 I105 global RPC identity correction GREEN

I105 synchronized only the controller artifact literal from Section 97.51 to
the exact Section 97.52 artifact before taking RED:

```text
pre-sync controller       76f97d7fe64a323958239a595d230357ad03082b822fa54ef466385ecd7834d2
synced vulnerable         a958924f0db8eb7542d639a1be1e8a3c52eb089b373e2eddfce2de90f8c15250
artifact                  10f8edba8b54b925bb90ae56fd7391c0d267ca322ecf132d4071eb985ea951a0
```

A private-shadow genuine RED had one valid RPC control PASS while both
predicted cross-operation request/request and request/response alias attacks
were incorrectly accepted. No setup correction was required.

The controller-only correction preserves all existing per-operation
count/schema/type/thread/order/exact-once and per-bucket alias checks, then
flattens all five request-side ID classes and every response ID. It requires
global request uniqueness, global response uniqueness and complete
request/response disjointness.

After one bounded Ruff formatting change, the complete affected and full gate
sets were rerun:

```text
same-case GREEN                  2/2
expanded valid / attacks         1/1 / 98/98
legacy scalar attacks            50/50
same-bucket aliases              10/10
cross request/request            10/10
cross request/response           20/20
cross response/response          6/6
full pure                        2/27/8/6/4/15 = 62/62
static                           15/15
in-memory / out-of-root compile  PASS / PASS
Ruff / format                    PASS / PASS
runtime authentication / exact8  PASS / 8/8
```

Runtime authentication used explicit repository `PYTHONPATH`, the canonical
managed prefix and repository product sources. The active evidence root,
probe, product files, artifact, HEAD/upstream/index and relevant process set
did not drift. A compile cleanup scan initially included existing repo
`.venv` caches; it was corrected to the active evidence root, observed zero
active pycache and deleted nothing.

Frozen pre-ledger identity:

```text
controller  a49246e3839435218451e712e92d2ac6ff9f16f39c57c62c60e9baa7581715ac
size/mode   74874 / 0600 / nlink 1
probe       c554041f86772a76403d4a0536137f40d87d4e3b17ad99ea1275ae2dc8b9a6f8
size/mode   30523 / 0600 / nlink 1
artifact    10f8edba8b54b925bb90ae56fd7391c0d267ca322ecf132d4071eb985ea951a0
root        uid 501 / 0700 / exact two
```

All shadow paths were removed exactly. No main, `_execute`, Popen, daemon,
FreeCAD, GUI, network or signal ran; real invocation remains zero.

This append changes the artifact hash. Exactly one controller-literal sync to
this Section 97.53 identity is allowed. The resulting exact bytes then remain
immutable through fresh I106 sol-max and I107 terra-medium admission gates
and, only on dual GREEN, the unique real V02R invocation.

#### MRG1-S78

1. **Completed milestones:** I105 genuine RED, global RPC identity correction,
   expanded `99/99`, full `62/62` and runtime/repository gates are GREEN; real
   invocation remains zero.
2. **Next steps:** final literal sync, concurrent I106 sol-max and I107
   terra-medium gates, then unique real FreeCAD invocation on dual GREEN.
3. **Approved decisions:** A04 and A08-D01..D07 cover this controller-only
   closure and fresh admission; no product/UI change or new approval is
   required.
4. **Execution discipline:** immutable exact identity after sync, shadow-only
   review, no artifact append or mutation during the real run, zero retries
   after invocation consumption.

| Entry ID | Decision / approval | Commit / push | Gate evidence | Residual | Snapshot | State |
|---|---|---|---|---|---|---|
| MRG1-G1-C02-E56 | A08 I105 | `not-created`; forbidden | RED control 1/1 plus 2 accepted attacks; GREEN 99/99; full 62/62; runtime/repo PASS | fresh dual gate and unique real invocation open | MRG1-S78 | correction GREEN / final sync next |

### 97.54 I107 gate contamination and exact recovery

Fresh I106 sol-max and I107 terra-medium started against:

```text
artifact    89ea9318d4da23629cf2925c8a58a66d1d735fc9dea7dd6fbcda4dd8eec63042
controller  c45b577d826e6bee17ae3b7d00f4f14e6a9d58bd6b44e75059b17f2a7763f263
probe       c554041f86772a76403d4a0536137f40d87d4e3b17ad99ea1275ae2dc8b9a6f8
```

I107 passed the immutable identity, compile, Ruff and format checks but its
compile command mistakenly used default `py_compile` output paths. It created
exactly two cache files inside the protected active root:

```text
__pycache__/v02r_controller.cpython-313.pyc
size 95559 / mode 0600 / nlink 1
sha256 99aa0cf32e6bc31b9f889517c7e0e6e1df9ef1523bb19069ec636431788c946d

__pycache__/v02r_probe.cpython-313.pyc
size 40720 / mode 0600 / nlink 1
sha256 43c03636d484eae8198b2f77f0d9ac147e793c425647512e2ed8990fda9d120a
```

The reviewer correctly stopped and did not conceal the deviation. Controller
and probe source hashes were unchanged, no real process ran and invocation
remained zero. I106 was ordered to freeze stale before its dynamic hooks; it
confirmed zero writes and no admission result.

The controller verified the exact names, regular-file type, uid, modes,
nlinks, sizes and hashes above. It unlinked only those two generated files and
removed only their now-empty `__pycache__` directory. No recursive or broad
delete was used. The active root returned to exact-two and the controller and
probe hashes above remained exact.

This is a mechanical gate execution failure, not a candidate/product defect.
The replacement mechanical packet must forbid active-root `py_compile`
entirely: compile in memory or direct every `cfile` to a private shadow/output
root. A fresh sol-max reviewer must also restart because I106 froze stale.

#### MRG1-S79

1. **Completed milestones:** I107 contamination was fully observed, recorded
   and exactly removed; active identity is restored; I106 correctly froze
   stale; real invocation remains zero.
2. **Next steps:** synchronize the controller artifact literal once, then run
   fresh I108 sol-max and I109 terra-medium against the restored exact-two
   identity with all compile outputs outside the active root.
3. **Approved decisions:** exact cleanup and replacement gates are mechanical
   A08-D04/D05 recovery; product behavior and UI remain unchanged.
4. **Execution discipline:** no dynamic import or compile output may target
   the active evidence root; private shadow/output roots only; any
   contamination freezes the gate before real admission.

| Entry ID | Decision / approval | Commit / push | Gate evidence | Residual | Snapshot | State |
|---|---|---|---|---|---|---|
| MRG1-G1-C02-E57 | A08 I106/I107 | `not-created`; forbidden | I107 setup breaker with two exact pyc; exact cleanup; I106 stale zero-write | fresh replacement dual gate required | MRG1-S79 | identity restored / I108-I109 next |

### 97.55 I108 controller-module provenance NO-GO

Replacement gates used the restored and rebound identity:

```text
artifact    ff6c841e2562dc5611f0dc5b4aa1f97dcd72a8f9cb7fd1d5d3046daa76c00c93
controller  3b0ba6e08ede93f4c0a2ba269a8ce22a978995067f281b52e40721a7b9af1f55
probe       c554041f86772a76403d4a0536137f40d87d4e3b17ad99ea1275ae2dc8b9a6f8
```

I109 did not contaminate the root, but its packet omitted the exact external
controller/probe paths and the literal managed-prefix path. It searched
obvious repository candidates, found mismatched identities and correctly
stopped without importing, compiling or writing anything. This is a
mechanical packet setup failure, not a candidate finding.

I108 sol-max verified the global RPC correction and all prior controller
closures:

```text
valid RPC                            PASS
cross request/request                10/10 rejected
cross request/response               20/20 rejected
cross response/response              6/6 rejected
invalid request IDs                  25/25 rejected
post-return root preservation        2/2
generation residue/detail            2/2
```

It then found one Major: controller-side runtime and daemon module provenance
is ambient and not bound before the unique Popen. `_runtime_authentication`
imports `vibecad.runtime.paths/spec/status` from current `sys.path`; its
existing `_module_source_identity` helper has no call site. Those modules
select and attest the managed prefix, receipt, FreeCAD, FreeCADCmd and Python.
Two calls to the same ambient modules prove only self-consistency. The
artifact and exact-eight hashes do not cover these controller-side sources,
and probe-side validation occurs only after Popen.

The earlier I101 installed-package import demonstrates that wrong ambient
resolution is reachable. An incorrect or injected controller-side
`vibecad.runtime` could select a self-consistent executable/receipt identity;
the controller would then consume its one-shot invocation and bind the GUI to
that selected identity rather than to the frozen repository implementation.

Required closure is controller-only:

- establish and re-admit canonical repository `src` before controller-side
  VibeCAD imports;
- reject preloaded/loaded VibeCAD runtime or daemon modules outside their
  exact repository paths;
- bind hashes and source identities for `vibecad`, runtime
  `paths/spec/status` and controller-used daemon `adapters/state/bootstrap`;
  and
- serialize those identities into runtime and invocation evidence before the
  sole Popen.

I108's canonical tally is
`0 Critical / 1 Major / 0 Moderate / 0 minor`, NO-GO. It used only a private
shadow, cleaned it exactly and left artifact/controller/probe, exact-eight,
HEAD/upstream/index, root cardinality and process state unchanged. Real
invocation remains zero.

#### MRG1-S80

1. **Completed milestones:** global RPC identity and all earlier closures are
   independently GREEN; I108 confirmed one controller-module provenance
   Major; I109 is an inert packet setup failure; real invocation remains zero.
2. **Next steps:** I110 sol-high obtains an ambient-module genuine RED,
   implements repository-source/hash/evidence binding and reruns affected plus
   full gates. Fresh sol-max and terra-medium then repeat admission with exact
   external paths and managed prefix.
3. **Approved decisions:** A04 and A08-D01..D07 cover this controller proof
   chain closure; product, probe and UI remain frozen and no new approval is
   required.
4. **Execution discipline:** controller-only mutation, explicit exact paths,
   no active-root import/compile output, shadow-only hooks, no real process
   before fresh dual GREEN.

| Entry ID | Decision / approval | Commit / push | Gate evidence | Residual | Snapshot | State |
|---|---|---|---|---|---|---|
| MRG1-G1-C02-E58 | A08 I108/I109 | `not-created`; forbidden | I108 NO-GO `0/1/0/0`; prior closures GREEN; I109 inert setup fail | controller module provenance Major open | MRG1-S80 | controller correction required / I110 next |

### 97.56 I110 controller-module provenance correction GREEN

I110 synchronized only the controller artifact literal to the Section 97.55
artifact before RED:

```text
start controller           3b0ba6e08ede93f4c0a2ba269a8ce22a978995067f281b52e40721a7b9af1f55
synced vulnerable          852b24f52dd7f8aeadf9f0d7b25505449e126b5fe38f2ac93eda8e388d7d1260
artifact                   322dd8d5ba0543eb2db40fd5e66ac774cfd30bd6fb588d30998c06d5075f7aa2
```

Private-shadow RED proved that the canonical repository modules worked but
module evidence was absent, an ambient foreign package earlier on
`PYTHONPATH` influenced import, and a preloaded fake `sys.modules` tree was
accepted and could return fake runtime identity.

I110 added exact source-path/hash bindings for nine modules:

```text
vibecad
vibecad.runtime
vibecad.runtime.paths
vibecad.runtime.spec
vibecad.runtime.status
vibecad.daemon
vibecad.daemon.adapters
vibecad.daemon.state
vibecad.daemon.bootstrap
```

The controller now admits the canonical repository and `src` directory type,
owner, mode and stat identity; places that exact `src` first in import
resolution; rejects foreign preloads by module type, `SourceFileLoader`,
source/spec/file/loader/package paths and source ownership; and re-admits
exact path/hash after import. Later runtime and daemon uses rebind and reuse
the authenticated modules. JSON-safe `controller_modules` and
`repository_source` identities are serialized in runtime and invocation
evidence, and missing, extra, wrong-path, wrong-hash, symlink or drifted
evidence is rejected.

Final formatted evidence:

```text
module targeted / module focused      9/9 / 27/27
preload and source-hash drift reject  PASS
alias matrix                          99/99
full pure                             2/27/8/6/4/15 = 62/62
static                                15/15
affected repository pytest            30 passed
unset / hostile PYTHONPATH auth        PASS / PASS
runtime_before == runtime_after        PASS
memory / private-output compile        2/2 / 2/2
Ruff / format                          PASS / PASS
exact-eight / frozen modules           8/8 / 9/9
```

The first affected pytest collection lacked explicit repository
`PYTHONPATH` and ran zero tests; the corrected final-format run passed 30/30.
A symlink hook initially resolved its link before testing and was corrected
to pass the exact symlink entry. A shadow-cwd Ruff invocation used different
configuration; the exact repository-cwd command identified the controller
format change, after which every affected gate above was rerun and both files
were reported formatted.

Frozen pre-ledger identity:

```text
controller  c0873ea6bf42d4e0265519e51eede88a151815007d5c11e21e036d286eb026be
size/mode   85801 / 0600 / nlink 1
probe       c554041f86772a76403d4a0536137f40d87d4e3b17ad99ea1275ae2dc8b9a6f8
size/mode   30523 / 0600 / nlink 1
artifact    322dd8d5ba0543eb2db40fd5e66ac774cfd30bd6fb588d30998c06d5075f7aa2
root        uid 501 / 0700 / exact two
```

Controller/probe active pycache, relevant processes and real invocation were
zero. Probe, product, artifact, HEAD/upstream/index were unchanged. I110's
private shadows were moved recoverably to one exact Trash directory; no
active evidence path was moved or removed.

This append changes the artifact hash. One controller-literal sync to this
Section 97.56 identity is allowed, followed by immutable fresh I111 sol-max
and I112 terra-medium admission with exact external paths and managed prefix.

#### MRG1-S81

1. **Completed milestones:** I110 ambient/preload RED and repository
   module-source/hash/evidence binding are complete; all formatted gates are
   GREEN; real invocation remains zero.
2. **Next steps:** final literal sync; concurrent I111 sol-max and I112
   terra-medium; unique real FreeCAD gate only on dual GREEN.
3. **Approved decisions:** A04 and A08-D01..D07 cover this controller-only
   proof-chain closure and fresh admission; product/probe/UI remain frozen.
4. **Execution discipline:** exact external paths and prefix in every packet,
   no active-root import or compile output, shadow-only hooks, immutable bytes
   after sync and zero retry after invocation consumption.

| Entry ID | Decision / approval | Commit / push | Gate evidence | Residual | Snapshot | State |
|---|---|---|---|---|---|---|
| MRG1-G1-C02-E59 | A08 I110 | `not-created`; forbidden | module 9/9+27/27; alias99; full62; pytest30; runtime dual-env PASS | fresh dual gate and unique real invocation open | MRG1-S81 | correction GREEN / final sync next |

### 97.57 I111B loaded-closure and exact-prefix NO-GO

The rebound candidate reviewed by I111B was:

```text
artifact    60d494786641e374ea8613bd7570475a07ccb01b55117bedc93b89feb935a076
controller  537a341aa1ef7f2af0f494338ae556b0165ef69850916876f2760d83f179b643
probe       c554041f86772a76403d4a0536137f40d87d4e3b17ad99ea1275ae2dc8b9a6f8
```

The first sol-max reviewer task was rejected by a platform content
classifier before inspecting the candidate. I111B rephrased the same local
software architecture QA scope and completed the review. I112 independently
passed immutable identity, exact-two, memory/private-output compile,
Ruff/format, normal/unset/foreign-path runtime authentication, exact-eight
and the nine frozen module bindings. It also ran both affected repository test
files with `217 passed`. It correctly withheld a complete mechanical PASS
because the historical ephemeral `99/62` harness was not persisted and it
would not invent those results; it made no active write.

I111B returned `0 Critical / 2 Major / 0 Moderate / 0 minor`, NO-GO:

1. **Loaded repository provenance closure is incomplete.** Importing the
   frozen nine modules cold-loads 33 `vibecad*` modules. The other 24 are
   checked only for a repository-local source and current arbitrary hash;
   their expected hashes are neither frozen nor serialized in invocation
   evidence. A private-shadow fixture changed
   `vibecad.daemon.facade` from its expected source hash while keeping its
   repository path. Binding still succeeded with `loaded=33`,
   `evidenced=9`, `omitted=24`.
2. **The managed prefix is not exact.** `_runtime_authentication` requires an
   absolute, self-consistent prefix and binaries beneath it but does not
   compare it to the approved canonical prefix. A different self-consistent
   local prefix was accepted in a pure fixture.

The 24 omitted loaded modules span application, daemon, execution,
interaction, runtime and workflow dependencies. The probe rejects preloaded
repository-package substitution and normalizes repository path order, but its
source evidence relies on `inspect.getsourcefile` and file hash without exact
loader/spec/file/package-path validation.

Required closure:

- freeze the exact 33-module loaded closure by module name, canonical source,
  SHA-256, loader/spec/file and package-path rules;
- reject any loaded `vibecad*` outside that exact closure;
- serialize the complete closure in before/launch/after runtime and
  invocation evidence;
- strengthen probe module evidence with the same loader/spec provenance
  checks; and
- compare the provided managed-prefix string and resolved identity to the
  single approved canonical prefix before accepting receipt or binary
  evidence.

All other reviewed closures were GREEN: global RPC identity/order,
one-shot-before-Popen, process generation and exact-PID signal authority,
isolated-root registration/preservation, durable result/stdout equality,
document -> checkout -> client cleanup, review terminal reread, same-EUID
residue and screenshot identity. I111B used only a private shadow, moved it
recoverably to Trash and left the active exact-two identity unchanged. Real
invocation remains zero.

#### MRG1-S82

1. **Completed milestones:** I111B confirmed two module/prefix proof-chain
   Majors while all other high-risk closures passed; I112 supplied independent
   identity/runtime/tooling and 217-test evidence; real invocation remains
   zero.
2. **Next steps:** I113 sol-high obtains loaded-closure and alternate-prefix
   RED, freezes the exact 33-module manifest, strengthens probe loader/spec
   evidence, enforces the canonical prefix and reruns affected/full gates.
3. **Approved decisions:** A04 and A08-D01..D07 cover controller/probe
   evidence-script corrections; product behavior and UI remain frozen and no
   new approval is required.
4. **Execution discipline:** controller/probe-only mutation, private-shadow
   hooks and compile output, exact source/prefix identities, no real process
   before fresh dual GREEN.

| Entry ID | Decision / approval | Commit / push | Gate evidence | Residual | Snapshot | State |
|---|---|---|---|---|---|---|
| MRG1-G1-C02-E60 | A08 I111B/I112 | `not-created`; forbidden | I111B NO-GO `0/2/0/0`; I112 identity/runtime/tooling PASS + pytest217 | loaded closure and exact-prefix Majors open | MRG1-S82 | controller/probe correction required / I113 next |

### 97.58 I113 exact loaded closure, managed-prefix and probe provenance GREEN

I113 was implemented serially under the approved A08 boundary after the
existing agent-thread pool could not accept another implementation packet.
No product, fixture or repository test byte was changed during I113; only the
external V02R controller and probe candidates changed.

The governing genuine RED was I111B:

- a cold import loaded 33 `vibecad*` modules while only nine were frozen and
  evidenced;
- a repository-local hash drift in `vibecad.daemon.facade` was accepted;
- a different self-consistent managed prefix was accepted; and
- probe source evidence did not bind loader, spec, file and package paths.

The first I113 exact-prefix implementation also exposed a narrower
adversarial RED: the raw approved prefix with a trailing `/.` normalized to
the same `Path` and was accepted. The implementation was corrected to compare
the raw string before path normalization. Both the trailing-`/.` value and a
different alternate prefix are now rejected before receipt or binary
admission.

I113 freezes the complete observed 33-module import closure by exact module
name, repository path, source SHA-256, exact `ModuleType`,
`SourceFileLoader`, loader name/path/filename, spec name/origin, file path
and package path. Any missing or extra loaded `vibecad*` module is rejected.
The complete manifest is serialized in controller runtime and invocation
evidence and is required on repeated authentication.

The probe now emits the same expanded loader/spec/file/package provenance for
its own module, `vibecad`, bootstrap and the five product modules. The
controller validates every exact key and expected value before accepting a
probe result.

Final formatted pre-ledger evidence:

```text
loaded closure targeted                 33/33
extra loaded / source drift / partial   reject / reject / reject
module assertion matrix                 99/99
RPC alias matrix                        99/99
probe expanded identity                 PASS
probe identity negatives                3/3 rejected
managed runtime authentication          2/2 equal
alternate managed prefixes              2/2 rejected
static                                  15/15
memory / private-output compile          2/2 / 2/2
Ruff / format                            PASS / PASS
affected repository pytest               217 passed
exact-eight                              8/8
root / invocation / pycache              exact-two / 0 / 0
```

The historical I110 pure `62/62` harness was ephemeral and was not
reconstructed or represented as a new I113 result. I113 instead reran every
changed module/prefix/probe closure, the independent RPC alias matrix, the
static/process controls and all affected repository tests. The final fresh
sol-max and terra-medium admissions remain mandatory and must evaluate the
complete final files rather than inherit an invented harness result.

Two local validation-script mismatches were setup-only:

- the first static script compared the AST-rendered main guard with the wrong
  quote style and searched for an obsolete executable-identity label; and
- the first probe-negative script accepted only `RuntimeError`, while a
  deliberately nonexistent spec path was correctly rejected as
  `FileNotFoundError`.

The corrected scripts passed `15/15` and `3/3` without changing candidate
bytes. An earlier zsh cleanup helper also used the shell-reserved `status`
name; the exact private shadow was inspected and removed without recursive
deletion. No setup deviation touched the active evidence root or consumed the
real invocation.

Frozen pre-ledger identity:

```text
artifact    817493859eda904da786e72ce48b1dc783bea599da31daa85bc4349ff3505dc9
controller  292bc820f7ef698111f59513e26d62678ba68fe9da0042b69283785d33849c51
size/mode   91077 / 0600 / nlink 1
probe       9b7790a59da951de106b8b46dc0fe2e15e60c5273be25d8ba3e948ac10607d35
size/mode   32297 / 0600 / nlink 1
root        uid 501 / 0700 / exact two
HEAD        0e1d2b0db9523685dacbdb5261ac87a14c1dfbf6
upstream    0e1d2b0db9523685dacbdb5261ac87a14c1dfbf6
index       empty
```

This append changes the artifact hash. One literal-only controller sync to
this Section 97.58 identity is allowed, followed by immutable I114 sol-max
whole-file adversarial admission and I115 terra-medium mechanical admission.
Only dual GREEN may admit the unique zero-retry real V02R invocation.

#### MRG1-S83

1. **Completed milestones:** I113 closed the 33-module, exact-prefix and probe
   provenance Majors with targeted/module/RPC/static/tooling/runtime/test
   GREEN; real invocation remains zero.
2. **Next steps:** literal-only artifact sync; concurrent I114 sol-max and
   I115 terra-medium whole-file admission; unique real FreeCAD invocation only
   on dual GREEN.
3. **Approved decisions:** A04 and A08-D01..D07 cover this evidence-script
   correction and fresh admission; product behavior, UI and exact-eight remain
   frozen.
4. **Execution discipline:** exact external paths and canonical managed
   prefix, no active-root import/compile output, immutable bytes after sync,
   no real process before dual GREEN and zero retry after invocation
   consumption.

| Entry ID | Decision / approval | Commit / push | Gate evidence | Residual | Snapshot | State |
|---|---|---|---|---|---|---|
| MRG1-G1-C02-E61 | A08 I113 | `not-created`; forbidden | closure33; module99; alias99; static15; compile/Ruff/format; pytest217; runtime/exact8 PASS | fresh dual admission and unique real invocation open | MRG1-S83 | correction GREEN / final literal sync next |

### 97.59 I114 adversarial NO-GO and I115 mechanical PASS

The immutable I114/I115 candidate was:

```text
artifact    58e669b307acae6180550e300420b351b470c66444521ff2cdbd97bb23d51b3d
controller  f81a800e1fe0bb747521fd53590935971e6bb14baefdd4fa139906106be48b40
size/mode   91077 / 0600 / nlink 1
probe       9b7790a59da951de106b8b46dc0fe2e15e60c5273be25d8ba3e948ac10607d35
size/mode   32297 / 0600 / nlink 1
root        uid 501 / 0700 / exact two
```

I115 terra-medium returned independent mechanical PASS. It confirmed the
frozen identity before and after, memory and private-output compile `2/2`,
Ruff and format, static `15/15`, affected pytest `217 passed`, exact-eight,
33-module manifest/hash/loader/spec admission, two equal runtime
authentications, trailing-`/.` prefix rejection, empty index, equal
HEAD/upstream and zero process/temp/socket/invocation residue. Its first AST
display helper treated a variable command as a literal list and raised a
setup-only `TypeError`; the corrected assertion proved the Popen command
originated from the authenticated absolute FreeCAD path. No candidate byte
or active-root entry changed.

I114 sol-max returned `0 Critical / 2 Major / 0 Moderate / 0 minor`, NO-GO:

1. **The controller accepts a completely forged first preload.** A shadow
   populated all 33 expected names with `ModuleType` objects that had never
   executed the corresponding source, but supplied exact
   `SourceFileLoader`, spec, file, package, parent and on-disk hash metadata.
   `_bind_repository_vibecad()` accepted all 33 and emitted all 33 evidence
   entries. The controller does not distinguish its first cold load from
   later repeated binding.
2. **The probe does not authenticate its transitive `vibecad*` closure.**
   Root `vibecad` and bootstrap retained their expected loaders while a
   meta-path hook supplied `vibecad.daemon.adapters` through a foreign loader.
   Probe binding accepted it and later trusted its `LocalAgentClient`.
   Bootstrap imports also load daemon/client/state and other transitive
   modules that are not enumerated by the probe.

Required I116 closure:

- the controller's first binding must reject any preloaded `vibecad*`;
- after its own cold import it must seal the exact module object identities,
  and every repeated binding must require the same 33 objects in addition to
  the existing source/loader/spec/hash rules;
- the probe must freeze and validate the exact transitive `vibecad*` closure
  produced by its imports, including name/path/SHA/module/loader/spec/file and
  package identity, before any Workbench/client use; and
- pure shadows must reproduce both REDs and prove forged full preload,
  foreign transitive loader, missing, extra and repeated-object replacement
  rejection.

All other I114 whole-file closures had no finding: durable
one-shot-before-Popen, repeated runtime identity, process/executable
generation and exact-PID signal, isolated-root immediate registration and
preservation, durable stdout/result equality, global RPC alias/order,
document -> checkout -> client cleanup, process-generation residue and
screenshot identity.

Both reviewers left artifact/controller/probe, exact-eight,
HEAD/upstream/index and active root unchanged. Their hooks used `-B` private
shadows; real Popen, daemon, FreeCAD, GUI, network and signal counts remained
zero.

#### MRG1-S84

1. **Completed milestones:** I115 independently passed all mechanical gates;
   I114 verified the I111B fixes and found two remaining first-load/transitive
   provenance Majors; real invocation remains zero.
2. **Next steps:** I116 sol-high implements first-load object sealing and
   complete probe transitive closure with genuine shadow RED/GREEN; rerun
   affected/full gates, then fresh sol-max and terra-medium admission.
3. **Approved decisions:** A04 and A08-D01..D07 cover these controller/probe
   evidence-script corrections; no product/API/UI behavior changes and no new
   approval are required.
4. **Execution discipline:** controller/probe-only mutation, shadow-only
   hooks and compile output, exact source/object/prefix identities, no real
   process before fresh dual GREEN.

| Entry ID | Decision / approval | Commit / push | Gate evidence | Residual | Snapshot | State |
|---|---|---|---|---|---|---|
| MRG1-G1-C02-E62 | A08 I114/I115 | `not-created`; forbidden | I115 PASS; I114 NO-GO `0/2/0/0` | first-load module seal and probe transitive provenance Majors open | MRG1-S84 | I116 correction required |

### 97.60 I116 first-load and probe-transitive provenance GREEN

I116 used only `-B` private shadows and changed only the external V02R
controller and probe. Its two genuine REDs reproduced I114 exactly:

```text
controller forged full preload accepted   modules=33 / evidence=33
probe foreign transitive loader accepted  ForeignLoader executed
probe independent cold closure             33 modules
```

The first probe RED fixture omitted two exports required by the daemon
package initializer and failed during setup. Adding those two ephemeral
foreign-stub exports produced the valid RED above; it did not change candidate
bytes.

The controller now requires zero loaded `vibecad*` modules on its first
binding. It cold-imports and validates the exact 33-module closure, then seals
each exact module object. Repeated authentication requires the same exact name
set and `sys.modules[name] is sealed[name]` for all 33 entries in addition to
the existing source/hash/loader/spec/file/package and parent-binding rules.

The probe freezes the independently observed exact 33-module transitive
closure, requires zero preload, validates every module before any
Workbench/client use, seals the exact objects and revalidates the seal before
client instrumentation and final evidence. Its result now includes 33 exact
`identity.vibecad_modules` entries; the controller requires the exact key set
and validates every path, SHA, loader, spec, file and package value.

Targeted GREEN:

```text
controller full/partial preload, cold, repeat,
missing, extra, replacement, foreign loader      8/8
probe full/partial preload, foreign transitive,
cold, repeat, missing, extra, replacement         8/8
controller probe-evidence validation               4/4
module assertion matrix                           99/99
RPC alias matrix                                  99/99
static                                             15/15
memory / private-output compile                     2/2 / 2/2
Ruff / format                                      PASS / PASS
runtime twice equal / closure / repeat seal         PASS / 33 / 33
alternate prefixes                                 2/2 rejected
affected repository pytest                          217 passed
exact-eight                                        8/8
```

No historical ephemeral harness result was inherited or invented. Both
`99/99` matrices were regenerated and executed in I116. Private compile
outputs were individually unlinked and their exact empty directory removed;
the active evidence root remained exact-two without pycache.

Frozen pre-ledger identity:

```text
artifact    c00b5ae3da8160b230edaa7a5b4d0d27408572352841959f58600be7445fc881
controller  0891af53b23914817e645f22bbbd892c4227f74127ed40982b7120eeaf94c5e8
size/mode   92810 / 0600 / nlink 1
probe       dcad2cb622bce978bc54bb7ec78ac22af8583232b19b6f1a67fa486eaf669165
size/mode   42075 / 0600 / nlink 1
root        uid 501 / 0700 / exact two
HEAD        0e1d2b0db9523685dacbdb5261ac87a14c1dfbf6
upstream    0e1d2b0db9523685dacbdb5261ac87a14c1dfbf6
index       empty
```

Real Popen, daemon, FreeCAD, GUI, network and signal counts remained zero.
There was no invocation, process, socket, pycache or I116 private-temp residue.
Product, API, protocol and UI bytes were unchanged.

The two intentionally duplicated 33-module manifests are independent trust
boundaries. A future legitimate import-closure change must explicitly
re-freeze both. Python object sealing is not an atomic kernel mechanism, but
the objects are revalidated before each protected use and every ambiguity
fails closed.

This append changes the artifact hash. One literal-only controller sync is
allowed, followed by immutable I117 sol-max whole-file adversarial admission
and I118 terra-medium mechanical admission. Only dual GREEN may admit the
unique zero-retry real V02R invocation.

#### MRG1-S85

1. **Completed milestones:** I116 reproduced and closed both I114 Majors with
   first-load zero-preload/object sealing and probe complete transitive
   provenance; all targeted, matrix, static, tooling, runtime and affected
   tests are GREEN; real invocation remains zero.
2. **Next steps:** literal-only artifact sync; concurrent I117 sol-max and
   I118 terra-medium whole-file admission; unique real FreeCAD invocation only
   on dual GREEN.
3. **Approved decisions:** A04 and A08-D01..D07 cover this evidence-script
   correction and final admission; product behavior, API, protocol, UI and
   exact-eight remain frozen.
4. **Execution discipline:** exact external paths and canonical managed
   prefix, no active-root compile output, immutable bytes after sync, no real
   process before dual GREEN and zero retry after invocation consumption.

| Entry ID | Decision / approval | Commit / push | Gate evidence | Residual | Snapshot | State |
|---|---|---|---|---|---|---|
| MRG1-G1-C02-E63 | A08 I116 | `not-created`; forbidden | RED 2; targeted 8+8+4; module99; alias99; static15; compile/Ruff/format; runtime; pytest217; exact8 PASS | fresh dual admission and unique real invocation open | MRG1-S85 | correction GREEN / final literal sync next |

### 97.61 I117/I118 dual GREEN and unique V02R terminal failure

After the single final artifact-literal sync, the immutable real-run candidate
was:

```text
artifact    973d78236de02373906a5de7fbf9826cba24b9c7213871f8e9f1badd8e9ca194
controller  c778b909cc0807a07c731a14961fea5b668ba8ee5c0c036a8f3af325e0ab4e2f
size/mode   92810 / 0600 / nlink 1
probe       dcad2cb622bce978bc54bb7ec78ac22af8583232b19b6f1a67fa486eaf669165
size/mode   42075 / 0600 / nlink 1
root        uid 501 / 0700 / exact two before invocation
```

I117 sol-max returned whole-file GO with
`0 Critical / 0 Major / 0 Moderate / 0 minor`. It rejected full and partial
first preload, missing, extra, replaced, parent/loader/spec/file/package and
foreign-transitive variants for both controller and probe, and rejected
missing, extra and replaced 33-module probe evidence. It found no new issue in
one-shot, runtime, process/signal, isolated-root, stdout/result, RPC,
cleanup, residue or screenshot closures.

I118 terra-medium independently passed immutable identity, memory/private
compile, Ruff/format, static `15/15`, controller/probe targeted closure,
probe evidence closure, runtime twice-equal, false prefix rejection,
exact-eight, Git/index/status, zero pre-run residue and affected repository
pytest with `217 passed`. Both agents left the active root unchanged and real
invocation at zero.

The controller then consumed the one and only approved V02R invocation:

```text
run_id       v02r_1b5d6f5382d3ef2a70a025e49470a748
exit         1
invocation   3ed8f49bd78f551d95e3fc448aeb9436a1e9b720f9ad21e3041fe376f27ecff1
final        1539a4ad93dd8bdfd76215da9119d868cff33465f54daba14d600489548d6321
root record  9d8612b917d30e1a3f620d047c0aa7b29d099fba3ad0ca477338230a53c7a6ca
admission    ce94189e2acdafdf8a81e282ee4f8c865a3a94b671ce4e8b40aeae36540cb714
Popen        0
GUI/probe    0 / 0
signal       attempts 0 / sent 0 / ambiguous 0
error        review fixture did not reach awaiting_user_review
```

The exclusive invocation record was durably written before fixture/client
work, so V02R is consumed even though the sole FreeCAD Popen was never
reached. It must never be invoked again.

The runtime-before, artifact, controller, probe, repository source,
33-module closure, managed prefix/receipt/generation/binaries and exact-eight
identities all matched. The private root was registered before child use and
was moved into the evidence directory on failure.

Both public direct `create_box` operations actually succeeded:

- the auto-commit candidate was a valid `10 x 20 x 30` box with
  `valid_shape=true`, `solid_count=1` and volume `6000`;
- the require-review candidate was a valid `12 x 22 x 32` box with
  `valid_shape=true`, `solid_count=1` and volume `8448`; and
- each task produced exact `model.FCStd` and `model.step` artifacts whose
  hashes and sizes match its stored manifest.

Both acceptance reports nevertheless returned
`observed=null / outcome=unsupported` and followed the correct
`fail_verification -> rolling_back -> failed` workflow. The project HEAD
remained at its empty generation-zero baseline and no Draft was published.

I119/I120 sol-max forensics classified the failure as one controller-fixture
Major, not a C02 product, managed-runtime, daemon or workflow defect:

```text
criterion family/check  topology / valid_shape
incorrect target        model
real shape target       body
```

The verifier matches shape targets exactly. `model` is the FCStd artifact
target; the sealed shape observation is named `body`. Existing real
Task-Kernel tests use the same public direct-operation path with
`topology / valid_shape / target=body` and reach
`awaiting_user_review`. Reading `StepResult.after.valid_shape` directly or
adding a product alias is forbidden because either would bypass or weaken the
acceptance boundary.

Current machine state is safe: the recorded receipt PID is absent; no
FreeCAD, daemon or controller process is live; the original private-temp root
is absent; and the failed root is preserved owner-private beneath V02R
evidence. However V02R failed before its full post-run path:
`runtime_after={}`, `run_root_state=null`, cleanup records only
`fixture_client`, and the preserved root contains a stale receipt/socket.
Therefore its empty `residue` mapping is not a stable final-snapshot proof.
V02R proves runtime-before, private-root admission, real headless create-box,
artifacts, verifier fail-closed behavior and clean workflow rollback. It does
not prove GUI/plugin, HEAD/Draft documents, screenshot, review, authenticated
retirement or final residue.

The complete V02R evidence tree is now immutable. No file, socket, receipt,
secret or directory beneath it may be removed, reused, migrated or treated as
V03 authority.

### 97.62 Proposed authorization MRG1-G1-C02-A09

`MRG1-G1-C02-A09` authorizes exactly one successor attempt named in full
`MRG1-G1-C02-V03`. This name must not be confused with the future G1-V03
Accept/Reject-across-restart matrix owned by C03.

The proposed decisions are:

1. **A09-D01 — new durable identity.** Create exactly one new owner-private
   local evidence root:

   ```text
   /Users/wangtao/Documents/DevProject/vibecad-c02-evidence/MRG1-G1-C02-V03
   ```

   It starts with exactly `v03_controller.py` and `v03_probe.py`, both
   owner-private `0600`, nlink 1, beneath a `0700` root. The currently
   available external volume is not qualified for active owner/mode/nlink/
   fsync identity; a future external archive remains separate.
2. **A09-D02 — frozen product scope.** Product exact-eight, API, protocol,
   daemon, runtime, workflow, UI and repository tests remain byte-frozen.
   V03 may modify only the new external controller/probe and this artifact.
   The three standing excluded paths remain unread, unmodified and unstaged.
3. **A09-D03 — one semantic fixture correction.** Replace the criterion
   target `model` with `body`, rename its acceptance identity to
   `v03-valid-shape`, and mechanically re-namespace every V02R path, run ID,
   environment and evidence label to V03. The Draft box may use position
   `[60,0,0]` to match the existing real test and avoid irrelevant overlap.
   No product alias and no StepResult shortcut is allowed.
4. **A09-D04 — exact public fixture proof.** Before GUI launch the controller
   must re-read and bind:
   - auto task `succeeded`, candidate equals committed revision and project
     HEAD;
   - review task base equals that HEAD, status is
     `awaiting_user_review`, committed revision is null;
   - the unique required verdict is
     `valid-shape/pass/observed=true` with exact shape evidence;
   - Draft ID/revision/base/generation/manifest are exact; and
   - publishing the Draft leaves HEAD unchanged.
5. **A09-D05 — complete failure cleanup evidence.** Capture daemon identity
   immediately after fixture-client open. Every post-consumption failure,
   including fixture failure, must enter one finalization path that attempts
   authenticated client/daemon retirement, runtime-after authentication,
   stable process-generation residue proof and strict run-root state. An
   unknown cleanup outcome remains failure and cannot be hidden by an empty
   mapping.
6. **A09-D06 — genuine RED/GREEN.** Preserve V02R as the real integration RED.
   A private pure verifier test must prove `target=model` yields exact
   unsupported for a real `body` shape and `target=body` yields exact pass.
   A fake-public-client fixture gate must prove auto-commit/HEAD advance,
   require-review/HEAD neutrality, exact verdict and Draft identity. Full
   module/seal, RPC, process, root, evidence, screenshot, compile, Ruff,
   format, exact-eight and affected repository gates then rerun.
7. **A09-D07 — immutable dual admission.** After one artifact-literal sync,
   fresh `gpt-5.6-sol / max` whole-file adversarial review and independent
   `gpt-5.6-terra / medium` mechanical admission must both be GREEN on the
   exact final hashes.
8. **A09-D08 — one new invocation, zero retry.** V03 has exactly one
   invocation, consumed durably before fixture-client, daemon or FreeCAD
   work. Dual GREEN is required before launch. Success or failure is terminal;
   no automatic V04 and no second V03 invocation are authorized.
9. **A09-D09 — success evidence.** V03 success requires runtime
   before/launch/after equality, exact daemon/GUI generations, two distinct
   clean HEAD/Draft Preview Documents, queued worker/main-thread evidence,
   exact-once review, screenshot identity, document -> checkout -> client/
   worker -> authenticated daemon cleanup, strict run-root state and zero
   same-EUID generation residue.
10. **A09-D10 — closeout boundary.** Only V03 success may admit C02 exact
    staging, commit and push. V03 failure returns to the user with immutable
    evidence and a new decision; it cannot silently waive the real gate.

Approval wording:

> Approve `MRG1-G1-C02-A09` as recorded in the current artifact revision:
> authorize exactly one new `MRG1-G1-C02-V03` protected root and one
> zero-retry invocation under A09-D01..D10; preserve V02R immutable; freeze
> product exact-eight; permit only successor controller/probe and artifact
> changes; require fresh sol-max plus terra-medium GREEN before launch; and
> forbid automatic V04, product changes, staging, commit or push before V03
> success.

No A09 action is authorized until the user explicitly approves this exact
revision.

#### MRG1-S86

1. **Completed milestones:** I117 GO and I118 PASS admitted the exact V02R
   candidate; its unique invocation was consumed and failed before Popen;
   I119/I120 proved a controller-fixture target Major while product execution,
   artifacts and workflow fail-closed behavior worked.
2. **Next steps:** obtain explicit A09 approval. If approved, create and
   implement the exact V03 successor packet, rerun full and fresh dual gates,
   then consume its single invocation. Without approval, preserve evidence and
   stop.
3. **Active decisions:** A04 and A08 remain historical authority for V02R,
   which is terminal and immutable. A09-D01..D10 are proposed, not active.
4. **Execution discipline:** artifact-only approval gate; no V03 root or
   file, no controller/probe mutation, no process, no stage/commit/push until
   A09 is explicitly approved.

| Entry ID | Decision / approval | Commit / push | Gate evidence | Residual | Snapshot | State |
|---|---|---|---|---|---|---|
| MRG1-G1-C02-E64 | A08 I117/I118/V02R; A09 proposed | `not-created`; forbidden | I117 GO `0/0/0/0`; I118 PASS; V02R exit1/Popen0; real create/artifacts PASS; fixture target Major proven | GUI/plugin/review/screenshot/final residue unproven; V02R immutable; A09 not approved | MRG1-S86 | terminal V02R failure / A09 approval gate open |

### 97.63 MRG1-G1-C02-A09 accepted and V03 execution packet

The user explicitly replied `MRG1-G1-C02-A09` to the approval request bound
to artifact revision:

```text
f2beafe463a010ab0c21ac699c0e29936796e8e599ee6957d5eea70fbe8ee121
```

This activates A09-D01..D10 exactly as recorded in Section 97.62. It does not
authorize a V02R retry, product/API/protocol/UI changes, automatic V04,
staging, commit or push before V03 success.

The immutable V02R tree is additionally frozen without exposing its private
contents:

```text
entries                    65
regular files              31
sockets                     1
structural/content manifest 5bceafe35677fd026dec1f0a3b96f3f09fa10f8f1063d85abbb21dc36ef9a11b
```

The manifest binds each relative path, type, mode, uid, nlink and size, plus
SHA-256 for every regular file. V03 implementation and all gates must reproduce
this exact manifest before and after their work. No V02R entry may be opened
for mutation, removed, copied as runtime authority or reused.

The selected Codex adapter capability record is:

- `live capability declarations`: native plan updates, bounded exec with
  native session polling, filesystem patching, and collaboration
  follow-up/message/wait are declared available in this session.
- `observable behavior`: artifact approval, spawn-send-wait collaboration,
  bounded commands and native polling of the original V02R session have all
  been observed in this campaign.
- `environment identity`: Codex desktop on the local macOS workspace
  `/Users/wangtao/Documents/DevProject/vibecad`.
- `public configuration`: unrestricted local filesystem access, network
  enabled and approval policy `never`; none expands A09 scope.

Capability profile:

```text
approval: artifact-approval
delegation: spawn-send-wait
persistence: repo-artifact
process: native-session-poll
adapter: Codex
```

The I121 seven-section implementation packet is:

1. **Objective:** create the one approved V03 protected root and implement
   only the controller/probe successor under A09-D01..D06.
2. **Identity:** bind this post-approval artifact hash, equal
   HEAD/upstream `0e1d2b0db9523685dacbdb5261ac87a14c1dfbf6`,
   empty index, frozen product exact-eight and the V02R tree manifest above.
3. **Allowlist:** new external `v03_controller.py`, new external
   `v03_probe.py`, and controller-owned artifact updates only. Product and
   the three excluded paths remain frozen.
4. **Implementation:** mechanically re-namespace V02R to V03, apply the one
   `model -> body` acceptance correction, strengthen exact public fixture
   validation and make every post-consumption failure enter the complete
   cleanup/residue evidence path.
5. **Gates:** preserve V02R integration RED; pure model/body RED/control;
   fake-public-client fixture matrix; namespace/one-shot/failure-cleanup,
   module/seal/RPC/process/root/evidence/screenshot, compile/Ruff/format,
   exact-eight and affected repository gates.
6. **Circuit breakers:** any product/repository drift, V02R manifest drift,
   active V03 extra file, unexpected RED, real process or invocation stops
   I121. No real V03 launch is permitted during implementation.
7. **Return:** exact start/end identities, RED/GREEN evidence, setup
   deviations, residuals and zero real invocation; controller must not stage,
   commit or push.

I121 is routed to the approved standard implementation tier, mapped by the
current host to `gpt-5.6-sol / high`. Fresh I122 sol-max and I123 terra-medium
will be issued only after I121 is frozen and its ledger entry is synchronized.

#### MRG1-S87

1. **Completed milestones:** A09-D01..D10 are explicitly approved on
   artifact `f2beafe…`; V02R is terminal and its 65-entry tree is frozen.
2. **Next steps:** I121 creates and implements V03 without real launch;
   controller records/final-syncs it, then I122/I123 perform fresh dual
   admission. Only dual GREEN may consume the one V03 invocation.
3. **Active decisions:** A04 and A08 are historical; A09 is active exactly
   for one V03 root and one zero-retry invocation.
4. **Execution discipline:** standard implementation, deep adversarial and
   fast mechanical routing; exact allowlists; V02R immutable; product frozen;
   native-session-poll only for the eventual unique launch.

| Entry ID | Decision / approval | Commit / push | Gate evidence | Residual | Snapshot | State |
|---|---|---|---|---|---|---|
| MRG1-G1-C02-E65 | A09 accepted on `f2beafe…` | `not-created`; forbidden | V02R tree manifest frozen; V03 root absent; HEAD/upstream equal; index empty | I121 implementation, fresh dual admission and unique V03 invocation open | MRG1-S87 | approved / I121 next |

### 97.64 I121 V03 implementation freeze and bounded pre-admission correction

I121 created only the A09-authorized external successor files and did not
launch a fixture client, daemon, FreeCAD, GUI or probe. Its pre-ledger freeze
is:

```text
artifact                     b28cb9d11d5c829f2eadabf7272395a5a5a1ebc60ab3c377020c70acfde6cd23
controller                   06c9550ff88af0c4da180b5868de3db66aed58a12f416c914c48f8367e086955
controller bytes             118394
probe                        00d7d7b28c8537bbb049184c1fe4da81e7cb758dfa279972033de09022db2287
probe bytes                  42937
HEAD/upstream                0e1d2b0db9523685dacbdb5261ac87a14c1dfbf6
index                        empty
V03 invocation               absent
```

The V03 root is `0700` and contains exactly the controller and probe. Each is
`0600`, owner 501 and nlink 1. The product exact-eight and repository scope
remain frozen.

The successor applies the sole fixture semantic correction authorized by
A09-D03: topology `valid_shape` now targets `body`, the acceptance identity is
`v03-valid-shape`, and the review box is positioned at `[60,0,0]`. No product
alias, protocol change or StepResult shortcut was introduced.

The exact public fixture proof now binds:

- auto-commit task, candidate, committed revision and project HEAD;
- review-task base, `awaiting_user_review`, null committed revision, exact
  verdict and Draft identity while HEAD remains neutral;
- post-review exact task and project rereads, candidate/manifest identity and
  the expected task/project generation advances;
- client daemon ID to stable before/after boot-state receipt and exact PID
  generation; and
- queued object identity, kind, order, observer main-thread execution, global
  uniqueness and command/event separation.

All post-consumption paths share one finalization boundary. The evidence
directory is established before the exclusive invocation publication, the
publication tail itself is guarded, and cleanup independently attempts client
and authenticated daemon retirement, runtime-after authentication, stable
process-generation residue capture, strict run-root inspection and the
post-run V02R manifest check. Unknown or non-empty outcomes fail closed and
remain in durable `final.json`.

The bounded RED/GREEN evidence is:

- real verifier semantics: old `model` is exact `unsupported`; corrected
  `body` is exact `pass` with `observed=true`;
- fake public fixture: valid path passes and 12/12 identity, verdict, Draft
  and HEAD mutations are rejected;
- post-review: the old predicate accepts a minimal succeeded/stale-HEAD
  counterexample; the corrected predicate rejects 5/5 counterexamples;
- queued evidence: the old predicate accepts reverse, extra, alias and
  wrong-thread counterexamples; the corrected predicate rejects 4/4;
- daemon binding: the valid control passes and 3/3 swap, mismatch and PID
  counterexamples are rejected;
- publication-tail and six finalization-phase injections prove that durable
  failure evidence and all later cleanup attempts survive each fault; and
- the V02R manifest control passes while a drifted manifest is rejected.

The complete local gates passed: static 15/15, controller closure/seal 33/33,
module assertions 99/99, RPC aliases 99/99, two equal runtime
authentications, two false-prefix rejections, exact-eight 8/8, Ruff check and
format, in-memory compile 2/2, and 217 affected tests. The controller also
proved the immutable V02R summary before and after:

```text
entries                     65
regular files               31
sockets                      1
structural/content manifest  5bceafe35677fd026dec1f0a3b96f3f09fa10f8f1063d85abbb21dc36ef9a11b
```

Controller-owned independent replay reproduced the same V02R summary,
in-memory compile, Ruff results and `217 passed`. V03 remained exact-two with
no pycache, socket or invocation, and the process census found no related live
process.

Setup deviations were contained before freeze. Direct compile/import checks
created four controller-owned CPython cache files; those exact files and their
empty cache directory were removed, then cardinality and hashes were
re-frozen. The initial controller artifact literal was a 62-character packet
transcription; it was corrected to the independently measured 64-character
artifact SHA-256. Two test-harness expectations were also corrected without
changing the candidate semantics. A bounded sol-max pre-review found three
Major evidence-chain groups and one Moderate V02R time-of-check gap; the
direct A09 findings above were repaired with genuine counterexamples. A
proposal to duplicate the entire persisted task program as a second trust root
was explicitly kept as non-blocking future hardening because it is not
required by A09-D03/D04 and would expand the authorized semantic scope.

Two prematurely dispatched final reviewers were interrupted before completion
and their results are not evidence. The next permitted action is one artifact
ledger hash synchronization in the controller, followed by fresh whole-file
I122 sol-max and independent I123 terra-medium admission on the resulting
exact final bytes. Real V03 work remains forbidden until both are GREEN.

#### MRG1-S88

1. **Completed milestones:** I121 created and locally froze the V03
   controller/probe, repaired the bounded pre-review findings, reran all local
   gates and preserved V02R byte-for-byte.
2. **Next steps:** compute this ledger revision, synchronize only the
   controller artifact literal, freeze the resulting identities, then run
   fresh I122/I123 in parallel. Dual GREEN is still required before the
   unique V03 invocation.
3. **Active decisions:** A09-D01..D10 remain the sole V03 authority; product
   exact-eight and V02R remain immutable; no V04 or retry is authorized.
4. **Execution discipline:** one literal-only synchronization window, then
   immutable whole-file review; no real process, stage, commit or push before
   V03 success.

| Entry ID | Decision / approval | Commit / push | Gate evidence | Residual | Snapshot | State |
|---|---|---|---|---|---|---|
| MRG1-G1-C02-E66 | A09 I121 implementation freeze | `not-created`; forbidden | RED/GREEN matrices; static15; closure/seal33; module/RPC99; runtime2; exact8; pytest217; V02R manifest stable | artifact literal sync and fresh I122/I123 open; V03 invocation absent | MRG1-S88 | local GREEN / ledger sync next |

### 97.65 I122 final NO-GO and bounded I124 controller correction

The single post-I121 ledger synchronization produced the identities reviewed
by fresh I122/I123:

```text
artifact                     3fa9e79761247c10f77e300eaf83112dfaa8fa2c7fa855a4b90846b465b1640e
controller                   4d302835178e1c21ec7ab202e711d57d511f8248d96664af4fdf93877eef5853
controller bytes             118394
probe                        00d7d7b28c8537bbb049184c1fe4da81e7cb758dfa279972033de09022db2287
probe bytes                  42937
```

I123 terra-medium returned PASS: exact identities and permissions, exact-two
cardinality, in-memory compile, Ruff, 217 affected tests, static15,
closure/seal33, module/RPC99, runtime authentication, the bounded
RED/GREEN matrices, exact-eight and the V02R manifest all passed without a
write or real invocation.

I122 sol-max returned canonical **NO-GO**, tally
`0 Critical / 2 Major / 0 Moderate / 0 minor`:

1. The timeout escalation calls exact-PID `SIGKILL` inside an exception
   handler without an inner guard. A capture-to-signal race or permission
   error can therefore escape the outer cleanup handler and skip complete
   finalization, environment restoration and durable `final.json`.
2. Fixture-client open may start or connect the daemon before the grouped
   daemon-ID/token capture completes. If stable receipt or PID binding fails,
   the grouped assignment leaves no retirement authority and finalization
   omits the required authenticated retirement attempt.

Both are direct A09-D05 post-consumption cleanup defects in the external
controller. They are not product, protocol, runtime or probe findings. I122
found no other blocker in its bounded A09 review.

All reviewed identities remained exact after both gates. V03 remained
`0700`/exact-two with `0600`, uid 501, nlink-1 files; invocation, pycache and
socket counts remained zero. HEAD/upstream remained
`0e1d2b0db9523685dacbdb5261ac87a14c1dfbf6`, the index remained empty,
product exact-eight stayed exact and V02R remained
`65/31/1` with manifest
`5bceafe35677fd026dec1f0a3b96f3f09fa10f8f1063d85abbb21dc36ef9a11b`.
No related controller, probe, FreeCAD or daemon process was observed.

I124 is a bounded correction under the user's standing authority for
non-product implementation work:

1. **Objective:** repair only the two I122 cleanup findings without a real
   V03 invocation.
2. **Identity:** bind this post-NO-GO artifact revision, the identities above,
   equal HEAD/upstream, empty index, exact-eight and immutable V02R.
3. **Allowlist:** only external `v03_controller.py` plus the one artifact
   literal synchronization. Probe, product, V02R and excluded paths are
   immutable.
4. **Implementation:** guard wait/signal escalation so every error is
   recorded and complete finalization always runs; retain the client daemon
   identity immediately after open and make finalization independently retry
   stable authentication and authenticated retirement when the initial
   grouped capture fails.
5. **Gates:** genuine pure RED for signal failure escaping finalization and
   partial daemon binding omitting retirement; GREEN must prove durable
   `final.json`, later cleanup attempts and fail-closed status. Rerun the full
   I121 local suite.
6. **Circuit breakers:** any probe/product/V02R drift, extra V03 entry,
   invocation, real process, scope expansion or unexpected RED stops I124.
7. **Return:** exact identities, RED/GREEN and full gate results. Fresh
   sol-max and terra-medium must again review the corrected final bytes; no
   interrupted or prior gate result may be reused.

This correction reopens the failed candidate freeze but does not consume or
expand the single A09 invocation. The artifact-literal synchronization for
I124 is the only synchronization permitted before its fresh final dual gate.

#### MRG1-S89

1. **Completed milestones:** I123 passed; I122 rejected the exact candidate
   with two controller-only D05 Majors; all frozen repository and evidence
   identities remained unchanged and V03 invocation stayed absent.
2. **Next steps:** execute bounded I124 at sol-high, freeze its exact bytes,
   then rerun fresh I125 sol-max and I126 terra-medium in parallel. Only their
   dual GREEN can admit the unique V03 invocation.
3. **Active decisions:** A09 remains active and unconsumed; I124 may change
   only the controller and synchronize its artifact literal.
4. **Execution discipline:** genuine two-case RED/GREEN, full local
   regression, immutable probe/product/V02R, no real process and no
   stage/commit/push.

| Entry ID | Decision / approval | Commit / push | Gate evidence | Residual | Snapshot | State |
|---|---|---|---|---|---|---|
| MRG1-G1-C02-E67 | A09 I122/I123; bounded I124 | `not-created`; forbidden | I123 PASS; I122 NO-GO `0/2/0/0`; hashes and V02R stable; invocation0 | two controller cleanup Majors open; fresh correction gates required | MRG1-S89 | correction authorized / I124 next |

### 97.66 I124 correction, final dual GREEN and terminal V03 result

I124 repaired only the two I122 controller cleanup findings. The old
controller was first proven genuinely RED:

- an exact-signal `PermissionError` escaped with zero calls to complete
  finalization; and
- a client-open followed by grouped daemon-capture failure supplied no daemon
  retirement action to finalization.

The corrected controller independently records every GUI poll, wait, TERM,
KILL and final-wait outcome. No GUI-cleanup exception can escape complete
finalization. It also retains the daemon ID immediately after client open and
independently performs stable receipt/PID-generation authentication before
retirement. Authentication failure is recorded and never authorizes retirement
or signaling of an unbound target.

I124 GREEN included GUI-cleanup faults 5/5, partial daemon capture and
unauthenticated controls, finalization phase injection 6/6 and the complete
local suite. Its final identities were:

```text
artifact                     02002a0142cdaad4b100f86e5a4d7d3652228ceacc2a746d92b777c68e06808d
controller                   87ab393a15734ccdc4f2eda520c26918ee6920bc5c5c9d1f48f1d69dd494aa3b
controller bytes             125214
probe                        00d7d7b28c8537bbb049184c1fe4da81e7cb758dfa279972033de09022db2287
probe bytes                  42937
```

Fresh I125 sol-max returned GO with canonical tally `0/0/0/0`. Fresh I126
terra-medium returned PASS. Both bound the exact identities above, exact-two
permissions, invocation zero, product exact-eight, equal HEAD/upstream, empty
index and the immutable V02R manifest. No prior or interrupted result was
reused.

One final read-only process preflight initially rejected its own shell command
because its `pgrep` argument contained the controller path. It did not execute
the controller and invocation remained absent. A process-table check that
separated executable identity from arguments then passed.

The one A09-authorized V03 invocation was launched once through the original
native session. It is terminal:

```text
run ID                       v03_a06f0e40ce1d4cad11da36665606adbc
exit                         1
invocation                   34fb45c70ca336c60f649f4a2689823ee45e47e9ab53bb5d5a7a3d12c9b534ab
final                        2ead959ee9320c0f0281c704c32950c1011f23ab9f5c614d5d9f084ffaf34454
Popen / GUI / probe / signal 0 / 0 / 0 / 0
```

The public headless fixture succeeded before the failure:

- auto-commit `create_box` executed with `body` `valid_shape=true`, exact
  passing verdict, FCStd/STEP artifacts and committed project HEAD;
- the review `create_box` executed at `[60,0,0]`, produced a second valid
  FCStd/STEP candidate and published the exact Draft while HEAD stayed
  neutral; and
- all bound project, task, revision, generation, manifest, verdict and Draft
  identities passed the controller's public rereads.

The failure occurred in the second runtime authentication, before the sole
FreeCAD `Popen`:

```text
loaded VibeCAD closure is not exact:
missing=[]
extra=['vibecad.runtime.uninstall']
```

I127 sol-max forensics classified one **Major**, owned by the external
controller/probe module-seal inventory rather than product or environment.
`LocalAgentClient.open()` legitimately calls
`connect_or_start_local_kernel()`. Its uninstall-pending guard deliberately
imports `vibecad.runtime.uninstall` before connecting or starting the daemon.
The initial controller authentication sealed 33 modules, but the legitimate
client-open path expanded the controller process to 34. The later exact-33
authentication therefore failed closed. The omitted repository module is:

```text
path    src/vibecad/runtime/uninstall.py
sha256  5fc10521fa3b42276253b8dd22b62e28a668bf9323d1eb09cd0f5880183931b2
```

The same lazy import is an intentional runtime-removal synchronization guard
in product. No product correction is required. The probe carries the same
latent 33-module seal omission.

Failure evidence is complete for the primary error but cannot prove compliant
cleanup. Client close and environment restoration ran; V02R remained exact.
Stable daemon authentication, runtime-after authentication and strict run-root
inspection failed on the same seal error. The final same-EUID snapshot observed
the daemon generation still alive. Receipt PID 60450 was absent in repeated
post-run checks and no related live process remains, but that later quiescence
cannot be promoted retroactively into authenticated retirement or a stable
zero-generation final snapshot. The owner-private stale receipt/socket and
credentials therefore remain inert and immutable in the preserved V03 tree.

The complete terminal V03 evidence tree is frozen without exposing its
private contents:

```text
entries                     66
regular files               32
sockets                      1
structural/content manifest  cea797dbba9a48a64caffc6b60534983a28a4c80cb29dae9bc269698e2fc88a9
```

V03 must not be retried, cleaned, repurposed or treated as future authority.

### 97.67 Proposed authorization MRG1-G1-C02-A10

`MRG1-G1-C02-A10` would authorize exactly one successor attempt named
`MRG1-G1-C02-V04`. It is intentionally narrower than A09:

1. **A10-D01 — new identity.** Create exactly one new owner-private
   `/Users/wangtao/Documents/DevProject/vibecad-c02-evidence/MRG1-G1-C02-V04`
   root, initially exact-two `v04_controller.py` and `v04_probe.py`, root
   `0700`, files `0600`, uid owner and nlink 1.
2. **A10-D02 — immutable prior evidence.** Preserve V02R and V03 byte-for-byte
   with their frozen manifests. Product exact-eight, API, protocol, daemon,
   runtime, workflow and UI remain frozen. Excluded paths remain unread,
   unmodified and unstaged.
3. **A10-D03 — one inventory correction.** Mechanically re-namespace V03 to
   V04 and add only the exact repository
   `vibecad.runtime.uninstall` path/hash to both controller and probe module
   inventories. Deterministically load it into the initial exact seal so the
   lifecycle remains a stable exact-34 closure. Every other extra, alias,
   wrong source/hash or object replacement remains rejected.
4. **A10-D04 — bounded RED/GREEN.** Preserve V03 as the real integration RED.
   Pure tests must prove the old exact-33 seal rejects the legitimate lazy
   import, the corrected exact-34 seal passes before and after fake client
   open, and arbitrary inventory mutations still fail. Fake execution must
   advance past runtime-launch authentication and retain cleanup authority.
5. **A10-D05 — regression boundary.** Keep the I124 controller cleanup,
   public fixture, FIFO, result, screenshot, process-generation, run-root and
   immutable-manifest behavior unchanged. Rerun the affected 217 tests and
   existing mechanical gates; introduce no new generalized hardening.
6. **A10-D06 — proportionate dual admission.** Run one fresh sol-max
   diff-focused/direct-boundary review and one independent terra-medium
   mechanical admission in parallel on final hashes. No repeated whole-file
   research round is required unless a direct Critical/Major is found.
7. **A10-D07 — one invocation, zero retry.** Only dual GREEN permits exactly
   one V04 invocation. It is consumed before fixture client, daemon or
   FreeCAD work. Success or failure is terminal; no automatic V05.
8. **A10-D08 — closeout.** V04 success may admit exact C02 staging, tests,
   commit and push. Failure preserves immutable evidence and returns to the
   user for a new decision.

Approval wording:

> Approve `MRG1-G1-C02-A10` as recorded in the current artifact revision:
> authorize one exact-34 module-inventory successor `MRG1-G1-C02-V04`, one
> zero-retry invocation, frozen V02R/V03/product scope, bounded dual admission
> and C02 closeout only on success.

No V04 file, mutation, process or invocation is authorized until the user
explicitly approves A10.

#### MRG1-S90

1. **Completed milestones:** I124 local GREEN, I125 GO and I126 PASS admitted
   V03; its unique invocation was consumed and failed before Popen after the
   complete headless fixture; I127 classified the exact-33 inventory Major.
2. **Next steps:** obtain explicit A10 approval. If approved, implement the
   exact-34 V04 successor with bounded RED/GREEN and one proportionate dual
   admission, then consume its one invocation. Without approval, preserve all
   evidence and stop.
3. **Active decisions:** A09 is terminal and fully consumed. A10-D01..D08 are
   proposed only; no V04 or V05 is authorized.
4. **Execution discipline:** artifact-only approval gate; V02R/V03/product
   frozen; no stage/commit/push before a successful authorized successor.

| Entry ID | Decision / approval | Commit / push | Gate evidence | Residual | Snapshot | State |
|---|---|---|---|---|---|---|
| MRG1-G1-C02-E68 | A09 terminal V03; A10 proposed | `not-created`; forbidden | I125 GO `0/0/0/0`; I126 PASS; V03 fixture PASS; runtime-launch seal Major; Popen0; terminal manifests frozen | GUI/plugin/review screenshot and compliant cleanup still unproven; A10 not approved | MRG1-S90 | terminal V03 failure / A10 approval gate open |

### 97.68 MRG1-G1-C02-A10 accepted and I128 V04 packet

At `2026-07-31T01:26:26Z` the user explicitly replied:

```text
批准 MRG1-G1-C02-A10
```

This approval is bound to artifact revision:

```text
8f742aaccdbef9376a111f91e04cb884771bd8dc02808878774d100b208c6ecb
```

A10-D01..D08 are therefore active exactly as recorded in Section 97.67. The
approval does not authorize a V03 retry, product/API/protocol/runtime/UI
change, automatic V05, broad staging or a commit before V04 succeeds.

Controller recovery checks reproduced:

```text
branch                       codex/agent-stage3
HEAD/upstream                0e1d2b0db9523685dacbdb5261ac87a14c1dfbf6
index                        empty
V04 root                     absent
V02R                         65 / 31 / 1
V02R manifest                5bceafe35677fd026dec1f0a3b96f3f09fa10f8f1063d85abbb21dc36ef9a11b
V03                          66 / 32 / 1
V03 manifest                 cea797dbba9a48a64caffc6b60534983a28a4c80cb29dae9bc269698e2fc88a9
related live processes       0
```

No directory-scoped `AGENTS.md` or `CLAUDE.md` was observed in the repository.
The approved C02 exact-eight remain the only product/test changes, and the
excluded paths remain unread and unstaged.

The required capability evidence record uses exactly the permitted
evidence-source categories:

- `live capability declarations`: native plan projection, bounded command
  execution with controllable session polling, patching, and
  spawn/send/follow-up/wait collaboration are declared in the current Codex
  session.
- `observable behavior`: native plan updates, external-file patching,
  subagent dispatch/follow-up/wait, and polling an original native process
  session have all completed observably during this campaign.
- `environment identity`: Codex desktop on the local macOS workspace
  `/Users/wangtao/Documents/DevProject/vibecad`.
- `public configuration`: unrestricted local filesystem access, network
  enabled and approval policy `never`; none expands A10 scope.

Selected capability profile:

```text
approval: native-plan
delegation: spawn-send-wait
persistence: repo-artifact
process: native-session-poll
adapter: Codex
```

The repo artifact remains authoritative; native plan state is only a
projection. User routing overrides the adapter defaults for this campaign:
standard implementation uses `gpt-5.6-sol / high`, deep architecture/review
uses `gpt-5.6-sol / max`, and fast mechanical admission uses
`gpt-5.6-terra / medium`.

The I128 seven-section packet is:

1. **Authorization.** Artifact `8f742a…` and A10-D01..D08 are explicitly
   approved. This packet inherits higher-priority system, developer and user
   instructions, the existing allowlist, and the current host permission
   model and sandbox. The Skill, artifact and packet cannot grant or expand
   permission, elevate authority or bypass the host. Do not request A10
   approval again.
2. **Workspace anchor.** Use repository
   `/Users/wangtao/Documents/DevProject/vibecad`, branch
   `codex/agent-stage3`, HEAD/upstream `0e1d2b0…`, empty index, and no observed
   directory-scoped instruction file. Writes are limited to the new external
   V04 controller/probe; the controller alone owns artifact updates. Product
   exact-eight, V02R, V03 and excluded paths are prohibited scope. The current
   host permission model and sandbox remain binding.
3. **Context.** V03 proved the complete headless public fixture and failed
   before Popen only because its exact-33 inventory omitted the legitimate
   lazy runtime-uninstall module. V04 succeeds when controller and probe use a
   stable exact-34 repository closure, reject every other mutation, preserve
   all A09/I124 evidence boundaries and remain invocation-free during
   implementation.
4. **Steps and gates.** Create the owner-private exact-two V04 root;
   mechanically re-namespace V03; add only
   `vibecad.runtime.uninstall` with repository SHA
   `5fc10521fa3b42276253b8dd22b62e28a668bf9323d1eb09cd0f5880183931b2`
   to both inventories; deterministically load it in the initial seal.
   Capture genuine old-33 RED and corrected exact-34 GREEN, arbitrary-extra,
   alias, wrong-source/hash and object-replacement negatives, fake
   runtime-launch progress and cleanup authentication. Rerun prior focused,
   cleanup, module/RPC, runtime, exact-eight, Ruff/format/compile and 217-test
   gates. Reproduce both prior evidence manifests before and after.
5. **Execution discipline.** Use `spawn-send-wait`, standard tier mapped by
   the user to sol-high, and no real process profile during implementation.
   Any product/probe-unrelated change, V02R/V03 drift, extra V04 entry,
   invocation, real daemon/FreeCAD process, unexpected RED, index change or
   out-of-allowlist write is a circuit breaker.
6. **Delivery boundary.** The implementer may finish only the external
   controller/probe and local gates. It may not edit the artifact, start final
   reviewers, run V04, stage, commit or push. The controller verifies state,
   synchronizes the ledger literal, dispatches the bounded final dual gate and
   alone owns the unique native-session launch.
7. **Final report.** Return exact before/after hashes, sizes, modes,
   cardinality, RED/GREEN and full numeric gates, deviations, residuals,
   prior-manifest checks, invocation/process zero and final repository state.

The existing stage contract remains active:

- **Commit sequence:** only `MRG1-G1-C02-CLOSE`,
  `feat(workbench): preview managed head and draft`, with artifact plus frozen
  exact-eight, after successful V04.
- **Manual validation:** the single managed FreeCAD invocation must show
  distinct clean HEAD/Draft Preview Documents, exact review evidence,
  screenshot identity and compliant cleanup.
- **Budget:** one V04 root, one controller, one probe, one dual admission and
  one zero-retry real invocation.
- **Expected impact:** external proof-harness inventory only; no product
  behavior, protocol, UI or baseline change. Expected product baselines remain
  217 C02 tests and the existing C01 regression matrix.
- **Residual:** C02 GUI/plugin/review screenshot and compliant cleanup remain
  open until V04 exit 0; failure is terminal and requires a new decision.

#### MRG1-S91

1. **Completed milestones:** A10 is explicitly approved on artifact
   `8f742a…`; repository/evidence recovery identities and zero related process
   state are verified.
2. **Next steps:** execute I128 at sol-high without a real launch; controller
   verifies and records the freeze, synchronizes the artifact literal, then
   dispatches one bounded sol-max and one terra-medium final gate. Dual GREEN
   alone may consume V04.
3. **Approved decisions:** A10-D01..D08 exactly as approved; A09/V03 and
   V02R are terminal historical evidence; product exact-eight is frozen.
4. **Execution discipline:** native-plan projection, spawn-send-wait,
   repo-artifact, native-session-poll, Codex adapter; exact allowlists and
   circuit breakers above; no stage/commit/push before V04 success.

| Entry ID | Decision / approval | Commit / push | Gate evidence | Residual | Snapshot | State |
|---|---|---|---|---|---|---|
| MRG1-G1-C02-E69 | A10 accepted on `8f742a…`; I128 | `not-created`; forbidden | recovery identities exact; V04 absent; process0; V02R/V03 manifests exact | I128, bounded dual admission and unique V04 invocation open | MRG1-S91 | approved / I128 next |

### 97.69 I128 exact-34 V04 local freeze

I128 created only the A10-authorized V04 controller/probe and did not launch a
fixture client, daemon, FreeCAD, GUI or probe. Its pre-ledger identities are:

```text
artifact                     4246d19be6435b142056d3f692c0ee94d5d5ec5c1102dac79d3ad8900a70cfed
controller                   122292079023d7586a962efca239be4a60738667f06b801c313e570fcd8cac4b
controller bytes             125392
probe                        4773735a6d5163d76f5269988cf81bbdc29bf459b6c5bb28d585e7ba7b12d0ee
probe bytes                  43176
V04 invocation               absent
related live processes       0
```

The V04 root is `0700`, uid 501 and exact-two. Both files are `0600`, uid 501
and nlink 1. No pycache, socket, invocation or final file exists.

The genuine RED was reproduced independently for controller and probe: each
old exact-33 seal passed cold binding, then rejected the legitimate lazy
`vibecad.runtime.uninstall` import as the sole extra module. The corrected
controller adds only that exact repository path/hash to its deterministic
inventory loop. The corrected probe adds the same inventory entry and
deterministically imports it before establishing its first seal.

GREEN evidence:

- controller and probe both establish exact-34 cold closures and retain the
  same object seal after fake-client lazy import;
- 10/10 negatives reject, on both sides, arbitrary extra, parent alias, wrong
  path, wrong hash and same-source object replacement;
- fake runtime flow completes equal `runtime_before -> runtime_launch ->
  cleanup_auth` identities with closure34, exact-eight and artifact binding;
- inherited direct controls pass: signal 2/2, GUI cleanup 5/5, finalization
  6/6, FIFO control plus 4/4 negatives, daemon control plus 3/3 negatives and
  partial binding, and publication 2/2;
- static 15/15, Ruff check/format, in-memory compile 2/2 and 217 affected
  repository tests pass.

Controller-owned normalized comparison proved that V04 differs from V03 only
by V04 namespace/artifact identity, one uninstall inventory entry in each
file, and the probe's deterministic first-seal import. No other logic differs.
The controller independently reproduced in-memory compile, Ruff and
`217 passed`.

Prior evidence remained byte-for-byte exact before and after:

```text
V02R  65 / 31 / 1  5bceafe35677fd026dec1f0a3b96f3f09fa10f8f1063d85abbb21dc36ef9a11b
V03   66 / 32 / 1  cea797dbba9a48a64caffc6b60534983a28a4c80cb29dae9bc269698e2fc88a9
```

HEAD/upstream remain `0e1d2b0db9523685dacbdb5261ac87a14c1dfbf6`,
the index is empty and product exact-eight remain frozen. The only setup
deviation was an initial call to an unavailable system `python`, which exited
127 without a write or state change; all subsequent commands used
`python3` or the repository venv.

The next permitted action is one artifact-literal synchronization in the V04
controller, followed by one fresh bounded sol-max direct-boundary review and
one independent terra-medium mechanical admission on the resulting exact
hashes. No real launch is permitted before dual GREEN.

#### MRG1-S92

1. **Completed milestones:** I128 genuine RED/GREEN, exact-34 implementation,
   proportionate regressions and independent controller replay are GREEN;
   V04 remains invocation-free and prior evidence is exact.
2. **Next steps:** synchronize only the controller artifact literal, freeze
   final identities, dispatch bounded I129 sol-max and I130 terra-medium in
   parallel, then launch one V04 only on dual GREEN.
3. **Approved decisions:** A10-D01..D08 remain active; V02R/V03/product are
   immutable; no V05 is authorized.
4. **Execution discipline:** native-plan projection, spawn-send-wait,
   repo-artifact, native-session-poll; proportionate final review only; no
   stage/commit/push before successful V04.

| Entry ID | Decision / approval | Commit / push | Gate evidence | Residual | Snapshot | State |
|---|---|---|---|---|---|---|
| MRG1-G1-C02-E70 | A10 I128 local freeze | `not-created`; forbidden | RED2; exact34; negatives10; direct controls; static15; pytest217; manifests exact | artifact literal sync, I129/I130 and unique V04 invocation open | MRG1-S92 | local GREEN / sync next |

### 97.70 I129/I130 dual GREEN and terminal pre-invocation V04 failure

The one ledger-literal synchronization produced:

```text
artifact                     90f712fd4281c79504fa3af691f90e266c5b8acc10981179578ee311422ec4a4
controller                   af5e2e06af0849c500710116a4e1d232bff8ef1bc1bcd84683349155e301556a
controller bytes             125392
probe                        4773735a6d5163d76f5269988cf81bbdc29bf459b6c5bb28d585e7ba7b12d0ee
probe bytes                  43176
```

Fresh I129 sol-max returned GO with canonical tally `0/0/0/0`. Fresh I130
terra-medium returned PASS after one setup-only private normalization
assertion was diagnosed and corrected in memory. The corrected normalization
left zero residual diff, the candidate hashes never changed, and the initial
private directory was removed. I130 reproduced 217 affected tests, static
15/15, compile 2/2, Ruff, exact-eight and both prior manifests.

Both gates proved the exact-34 lifecycle, all 10 module negatives, equal fake
runtime identities and unchanged direct cleanup/finalization boundaries.
Controller preflight then independently rebound the identities, exact-two
root, equal HEAD/upstream, empty index, absent invocation and zero related
processes.

The one A10-authorized controller run exited 1 before creating durable
`invocation.json`, an evidence directory, a fixture client, daemon or FreeCAD
process:

```text
IncompleteProcessRead:
  pid=66505
  stage=snapshot_executable_first
  observed=0
  errno=2
```

The failure occurred in the initial same-EUID baseline snapshot. The V04 root
therefore remains exact-two and contains no invocation or final evidence. Its
terminal structural/content identity is:

```text
entries                     3
regular files               2
sockets                     0
structural/content manifest d8d7478c353eeccbeef691c2f55a85bca7d3abdd35c39d562ced989642909fb6
```

PID 66505 is a pre-existing uid-501 Codex `ChatGPT for Chrome` extension-host
process. Three independent Darwin samples produced the same complete 136-byte
BSD generation identity, PID/PPID/UID/PGID/SID and birth time. Only
`proc_pidpath` returned zero with `ENOENT` on every sample. The process remained
live and unchanged after V04 exited. V04 performed no fixture, client, daemon,
FreeCAD, Popen or signal action.

I131 sol-max classified one **Major**:

```text
Critical 0 / Major 1 / Moderate 0 / minor 0
```

The owner is an environment-triggered controller proof-policy incompatibility,
not product, FreeCAD, daemon or Chrome. `_capture_uid_snapshot` routes every
same-EUID PID through strict `_capture_process_token`, which requires two exact
executable-path reads even for unrelated pre-existing baseline generations.
The eight-attempt stable snapshot correctly fails closed but cannot form a
baseline around a path-opaque process despite its complete stable BSD/SID
identity.

Closing the observed extension host is not a sufficient policy: it cannot
authorize a V04 retry, the host may respawn, and another same-EUID path-opaque
process could consume the next attempt. V04 is terminal and immutable.

### 97.71 Proposed authorization MRG1-G1-C02-A11

`MRG1-G1-C02-A11` would authorize exactly one successor
`MRG1-G1-C02-V05` and only the following process-baseline correction:

1. **A11-D01 — new identity.** Create one owner-private V05 root, initially
   exact-two `v05_controller.py` and `v05_probe.py`, root `0700`, files
   `0600`, current owner and nlink 1.
2. **A11-D02 — frozen scope.** Preserve V02R, V03 and V04 byte-for-byte with
   their manifests. Freeze product exact-eight, API, protocol, runtime,
   FreeCAD UI and exact-34 module inventory. Excluded paths remain unread,
   unmodified and unstaged.
3. **A11-D03 — narrow baseline policy.** Mechanically re-namespace V04 to V05.
   Change only the external controller process census:
   - full identical two-read BSD generation and SID remain mandatory for every
     PID;
   - exact executable identity remains mandatory for the controller, every
     required token, daemon, GUI, executable binding and every pre-signal
     recapture;
   - only a non-required, pre-existing baseline generation may record explicit
     executable-unavailable evidence when both executable probes return the
     same observed-byte/errno tuple inside an otherwise identical BSD/SID
     capture;
   - final census may use that state only for the exact matching baseline
     BSD/SID generation and the same unavailable tuple;
   - incomplete BSD identity, new opaque PID, PID reuse, birth/PPID/PGID/SID
     change, available/unavailable transition or unavailable-evidence change
     remains fail-closed;
   - an executable-unavailable token can never authorize a signal.
   Probe logic remains unchanged apart from namespace/artifact identity.
4. **A11-D04 — genuine RED/GREEN.** Preserve V04 as the real integration RED.
   Pure RED must reproduce stable BSD136 plus repeated path `0/ENOENT` abort
   and snapshot exhaustion. GREEN must admit only the exact stable unrelated
   baseline generation. Controls must reject incomplete BSD; required,
   controller, daemon, GUI and pre-signal path-unavailable tokens; mismatched
   unavailable tuples; generation changes; PID reuse; new opaque PIDs; and
   all signal authority from optional tokens. Structured unavailable evidence
   must survive final serialization.
5. **A11-D05 — proportionate regressions and admission.** Normalized V05 diff
   must contain only namespace/literal and this policy. Rerun process/static,
   exact-34 runtime, finalization, exact-eight, 217 tests and immutable
   manifests. Then run one fresh sol-max diff/direct-boundary review and one
   terra-medium mechanical admission in parallel.
6. **A11-D06 — one invocation, zero retry.** Only dual GREEN permits one V05
   controller run. Success or failure is terminal; no automatic V06.
7. **A11-D07 — success-only closeout.** V05 exit 0, screenshot inspection and
   compliant cleanup admit exact C02 staging, tests, commit and push. Failure
   preserves evidence and returns to the user.

Approval wording:

> Approve `MRG1-G1-C02-A11` as recorded in the current artifact revision:
> authorize one V05 successor with the narrow stable-BSD baseline policy,
> frozen product/prior evidence, proportionate dual admission and one
> zero-retry invocation; forbid automatic V06.

No V05 file, mutation, process or invocation is authorized until the user
explicitly approves A11.

#### MRG1-S93

1. **Completed milestones:** I129/I130 admitted exact-34 V04; its unique
   controller run failed before durable invocation or any product process;
   I131 proved one persistent path-opaque baseline-policy Major.
2. **Next steps:** obtain explicit A11 approval. If approved, implement only
   the V05 controller baseline-token policy with genuine RED/GREEN, run one
   proportionate dual gate and then one terminal invocation. Without approval,
   preserve evidence and stop.
3. **Active decisions:** A10/V04 is terminal. A11-D01..D07 are proposed only;
   V02R/V03/V04/product are immutable and no V05/V06 is authorized.
4. **Execution discipline:** artifact-only approval gate; no old-evidence
   cleanup, no environment workaround presented as proof, and no
   stage/commit/push before a successful authorized successor.

| Entry ID | Decision / approval | Commit / push | Gate evidence | Residual | Snapshot | State |
|---|---|---|---|---|---|---|
| MRG1-G1-C02-E71 | A10 terminal V04; A11 proposed | `not-created`; forbidden | I129 GO `0/0/0/0`; I130 PASS; V04 exit1 pre-invocation; BSD stable/path opaque Major | GUI/plugin/review screenshot and compliant cleanup unproven; A11 not approved | MRG1-S93 | terminal V04 failure / A11 approval gate open |

### 97.72 MRG1-G1-C02-A11 accepted and I132 V05 packet

At `2026-07-31T02:22:48Z` the user explicitly replied:

```text
批准 MRG1-G1-C02-A11
```

The user additionally confirmed that work which does not change product
requirements may proceed autonomously. The approval is bound to artifact
revision:

```text
8a12ba0a25b70738bd96b9308d55f9604c2dec8da06675426a00c5f86c324576
```

A11-D01..D07 are therefore active exactly as recorded in Section 97.71. The
approval does not authorize a V04 retry, product/API/protocol/runtime/UI
change, automatic V06, broad staging or a commit before V05 succeeds.

Controller recovery checks reproduced:

```text
branch                       codex/agent-stage3
HEAD/upstream                0e1d2b0db9523685dacbdb5261ac87a14c1dfbf6
index                        empty
V05 root                     absent
V02R                         65 / 31 / 1
V02R manifest                5bceafe35677fd026dec1f0a3b96f3f09fa10f8f1063d85abbb21dc36ef9a11b
V03                          66 / 32 / 1
V03 manifest                 cea797dbba9a48a64caffc6b60534983a28a4c80cb29dae9bc269698e2fc88a9
V04                          3 / 2 / 0
V04 manifest                 d8d7478c353eeccbeef691c2f55a85bca7d3abdd35c39d562ced989642909fb6
related live processes       0
```

No directory-scoped `AGENTS.md` or `CLAUDE.md` was observed in the repository.
The approved C02 exact-eight remain the only product/test changes, and the
excluded paths remain unread and unstaged.

The required capability evidence record uses exactly the permitted
evidence-source categories:

- `live capability declarations`: native plan projection, bounded command
  execution with controllable session polling, patching, and
  spawn/send/follow-up/wait collaboration are declared in the current Codex
  session.
- `observable behavior`: native plan updates, external-file patching,
  subagent dispatch/follow-up/wait, and polling an original native process
  session have all completed observably during this campaign.
- `environment identity`: Codex desktop on the local macOS workspace
  `/Users/wangtao/Documents/DevProject/vibecad`.
- `public configuration`: unrestricted local filesystem access, network
  enabled and approval policy `never`; none expands A11 scope.

Selected capability profile:

```text
approval: native-plan
delegation: spawn-send-wait
persistence: repo-artifact
process: native-session-poll
adapter: Codex
```

The repo artifact remains authoritative; native plan state is only a
projection. User routing remains: standard implementation uses
`gpt-5.6-sol / high`, deep architecture/review uses
`gpt-5.6-sol / max`, and fast mechanical admission uses
`gpt-5.6-terra / medium`.

The I132 seven-section packet is:

1. **Authorization.** Artifact `8a12ba0…` and A11-D01..D07 are explicitly
   approved. This packet inherits higher-priority system, developer and user
   instructions, the existing allowlist, and the current host permission
   model and sandbox. The Skill, artifact and packet cannot grant or expand
   permission, elevate authority or bypass the host. Do not request A11
   approval again.
2. **Workspace anchor.** Use repository
   `/Users/wangtao/Documents/DevProject/vibecad`, branch
   `codex/agent-stage3`, HEAD/upstream `0e1d2b0…`, empty index, and no observed
   directory-scoped instruction file. Writes are limited to the new external
   V05 controller/probe; the root controller alone owns artifact updates.
   Product exact-eight, V02R, V03, V04 and excluded paths are prohibited
   scope. The current host permission model and sandbox remain binding.
3. **Context.** V04 passed exact-34 dual admission but its unique controller
   run stopped before durable invocation because one unrelated pre-existing
   same-EUID process had complete, stable two-read BSD/SID generation identity
   while both executable-path reads returned the same `0/ENOENT`. V05
   succeeds when this exact baseline state is recorded without weakening any
   required-process, new-process, generation, executable-binding or
   pre-signal boundary.
4. **Steps and gates.** Create the owner-private exact-two V05 root and
   mechanically re-namespace V04. Change only controller baseline-census
   handling so an unrelated, non-required, pre-existing generation may carry
   structured stable executable-unavailable evidence. Keep complete two-read
   BSD/SID mandatory and exact executable identity mandatory everywhere
   listed in A11-D03. Capture genuine V04-policy RED and corrected GREEN;
   reject incomplete BSD, required/controller/daemon/GUI/pre-signal
   unavailable identity, tuple mismatch, availability transition, generation
   change, PID reuse, new opaque PID and every signal attempt from an optional
   token. Preserve structured evidence through final serialization. Rerun
   process/static, exact-34 runtime, finalization, exact-eight, Ruff,
   format/compile, 217 affected tests and all three immutable manifests.
5. **Execution discipline.** Use `spawn-send-wait`, standard tier mapped by
   the user to sol-high, and no real process profile during implementation.
   Any product/probe-logic change, prior-evidence drift, extra V05 entry,
   invocation, real daemon/FreeCAD process, unexpected RED, index change or
   out-of-allowlist write is a circuit breaker.
6. **Delivery boundary.** The implementer may finish only the new external
   V05 controller/probe and local gates. The probe may differ from V04 only by
   namespace/artifact identity. It may not edit the artifact, start final
   reviewers, run V05, stage, commit or push. The root controller verifies
   state, synchronizes the ledger literal, dispatches one bounded sol-max
   diff/direct-boundary review and one terra-medium mechanical admission, and
   alone owns the unique native-session launch.
7. **Final report.** Return exact before/after hashes, sizes, modes,
   cardinality, RED/GREEN and full numeric gates, deviations, residuals,
   prior-manifest checks, invocation/process zero and final repository state.

The existing stage contract remains active:

- **Commit sequence:** only `MRG1-G1-C02-CLOSE`,
  `feat(workbench): preview managed head and draft`, with artifact plus frozen
  exact-eight, after successful V05.
- **Manual validation:** the single managed FreeCAD invocation must show
  distinct clean HEAD/Draft Preview Documents, exact review evidence,
  screenshot identity and compliant cleanup.
- **Budget:** one V05 root, one controller, one probe, one proportionate dual
  admission and one zero-retry real invocation.
- **Expected impact:** external process-baseline proof policy only; no product
  behavior, protocol, UI or baseline change. Expected product baselines remain
  217 C02 tests and the existing C01 regression matrix.
- **Residual:** C02 GUI/plugin/review screenshot and compliant cleanup remain
  open until V05 exit 0; failure is terminal and requires a new decision.

#### MRG1-S94

1. **Completed milestones:** A11 is explicitly approved on artifact
   `8a12ba0…`; repository/evidence recovery identities, absent V05 and zero
   related-process state are verified.
2. **Next steps:** execute I132 at sol-high without a real launch; root
   verifies and records the freeze, synchronizes the artifact literal, then
   dispatches one bounded sol-max and one terra-medium final gate. Dual GREEN
   alone may consume V05.
3. **Approved decisions:** A11-D01..D07 exactly as approved; A10/V04,
   A09/V03 and V02R are terminal historical evidence; product exact-eight is
   frozen.
4. **Execution discipline:** native-plan projection, spawn-send-wait,
   repo-artifact, native-session-poll, Codex adapter; exact allowlists and
   circuit breakers above; no stage/commit/push before successful V05.

| Entry ID | Decision / approval | Commit / push | Gate evidence | Residual | Snapshot | State |
|---|---|---|---|---|---|---|
| MRG1-G1-C02-E72 | A11 accepted on `8a12ba0…`; I132 | `not-created`; forbidden | recovery identities exact; V05 absent; process0; V02R/V03/V04 manifests exact | I132, proportionate dual admission and unique V05 invocation open | MRG1-S94 | approved / I132 next |

### 97.73 I132 V05 narrow baseline-policy local freeze

I132 created only the A11-authorized V05 controller/probe and did not launch a
fixture client, daemon, FreeCAD, GUI or probe. Its pre-ledger identities are:

```text
artifact                     024ab95d9eddcb377f663d26edcd01c99733b975549b9b958a88da7ab9777d0b
V04 controller source        af5e2e06af0849c500710116a4e1d232bff8ef1bc1bcd84683349155e301556a
mechanical V05 controller    d54536e4056b19572e4c21d343ab7e54b44cbffa93481f559838c5643f45e841
final V05 controller         5c44bdd2828e6bfc5fddc16ae47ad262cc93a8867af90ceb84e7535a0caeef75
controller bytes             138015
V04 probe source             4773735a6d5163d76f5269988cf81bbdc29bf459b6c5bb28d585e7ba7b12d0ee
final V05 probe              7c1ba0a7fb94cf9503c63b687b2892ff94f156e56be482e35c1caacbbd309f8a
probe bytes                  43176
V05 manifest                 3 / 2 / 0
V05 manifest sha256          b93827ff7654d00dd0d3633423929085508f73531be266f59263b3ca0df22bbc
```

The V05 root is `0700`, uid 501 and exact-two. Both files are `0600`, uid 501
and nlink 1. No pycache, socket, invocation, final file or extra entry exists.

The controller uses a separate `UnavailableProcessToken` for the narrow
optional state instead of weakening the required `ProcessToken`. Every
optional capture still performs BSD, executable, BSD, executable reads and
requires equal full BSD/SID generation plus equal unavailable
`observed_bytes/errno`. Initial census makes the controller PID an explicit
strict-executable required token. Final census receives the original baseline:
only the exact unavailable generation and tuple can be reused; every new PID
and every originally available token remains strict.

Daemon, GUI, GUI executable binding, required-token recapture, signal target
and every pre-signal recapture require exact executable identity. An optional
token loses signal authority before any injected capture or kill can run.
The canonical unavailable-generation mapping is included in both the future
invocation and final serialization.

The genuine RED/GREEN and bounded controls were:

```text
V04-policy RED                              3/3
stable optional baseline GREEN              2/2
adversarial process-policy controls         24/24
final exact unavailable reuse               1/1
exact-34 controller/probe authentication     PASS / PASS
signal exact controls                        2/2
finalization fault containment               6/6
residue controls                             2/2
structured invocation serialization          1/1
static                                       20/20
normalized probe residual                    0
Ruff / format / in-memory compile            PASS / 2/2 / 2/2
affected repository pytest, implementer      217 passed in 1.98s
affected repository pytest, controller       217 passed in 1.95s
product exact-eight                          8/8
```

RED reproduced two complete stable 136-byte BSD reads with repeated executable
path `0/ENOENT` aborts and exact four-attempt snapshot exhaustion. GREEN admits
only the exact stable unrelated baseline. The 24 negatives cover incomplete
BSD, unavailable-tuple mismatch, both availability transitions, every
PID/PPID/birth/usec/EUID/PGID/SID mutation, PID reuse, final tuple drift, a new
opaque PID, required/controller/daemon/GUI/binding/pre-signal unavailability
and zero signal authority for an optional token.

The normalized probe is identical to V04 after only namespace and artifact
identity. The controller has 27 policy/literal regions, `+359/-25`, all within
A11-D03 or V05 identity. Two pre-freeze draft tooling findings, Ruff E731 and
one formatter delta, were mechanically corrected inside the controller
allowlist; final bytes pass all tooling.

Controller-owned independent recovery reproduced:

```text
V02R  65 / 31 / 1  5bceafe35677fd026dec1f0a3b96f3f09fa10f8f1063d85abbb21dc36ef9a11b
V03   66 / 32 / 1  cea797dbba9a48a64caffc6b60534983a28a4c80cb29dae9bc269698e2fc88a9
V04    3 /  2 / 0  d8d7478c353eeccbeef691c2f55a85bca7d3abdd35c39d562ced989642909fb6
```

HEAD/upstream remain `0e1d2b0db9523685dacbdb5261ac87a14c1dfbf6`,
the index is empty, product exact-eight are frozen and related live processes
are zero.

The next permitted write is one controller artifact-literal synchronization.
After that write the artifact remains read-only while one fresh sol-max
diff/direct-boundary review and one independent terra-medium mechanical
admission inspect the exact final identities in parallel. No real launch is
permitted before dual GREEN.

#### MRG1-S95

1. **Completed milestones:** I132 genuine RED/GREEN, 24 negative controls,
   exact-34/runtime/finalization/static gates, independent 217-test replay and
   all four manifests are GREEN; V05 remains invocation-free.
2. **Next steps:** synchronize only the controller artifact literal, freeze
   final identities, dispatch bounded I133 sol-max and I134 terra-medium in
   parallel, then launch one V05 only on dual GREEN.
3. **Approved decisions:** A11-D01..D07 remain active; V02R/V03/V04/product
   are immutable; no V06 is authorized.
4. **Execution discipline:** native-plan projection, spawn-send-wait,
   repo-artifact, native-session-poll; proportionate final review only; no
   stage/commit/push before successful V05.

| Entry ID | Decision / approval | Commit / push | Gate evidence | Residual | Snapshot | State |
|---|---|---|---|---|---|---|
| MRG1-G1-C02-E73 | A11 I132 local freeze | `not-created`; forbidden | RED3; GREEN2; negatives24; static20; pytest217x2; manifests exact | artifact literal sync, I133/I134 and unique V05 invocation open | MRG1-S95 | local GREEN / sync next |

### 97.74 I133/I134 dual GREEN and terminal pre-main V05 failure

The one ledger-literal synchronization produced:

```text
artifact                     93693b51d45c6c37a417575fb13bfb1c202b85ee2b507ab356476cdfe2b0120e
controller                   7920bfcfa3789c7833ff94672dd030ded79f5caabbb4bcf9593b9c4983ea60aa
controller bytes             138015
probe                        7c1ba0a7fb94cf9503c63b687b2892ff94f156e56be482e35c1caacbbd309f8a
probe bytes                  43176
V05 manifest                 3 / 2 / 0
V05 manifest sha256          b819b694a8015565355797fc94fc6d24d5f2ba574960ce38abacf3403f31ac46
```

Fresh I133 sol-max returned GO with canonical tally `0/0/0/0`. It independently
reproduced RED `3/3`, GREEN `2/2`, negatives `24/24`, forged optional-token
signal `0`, structured durable mapping and original-baseline final census.
The probe normalized to zero residual. The controller normalized to only five
new policy definitions and ten changed in-scope functions, with no removed
definition or out-of-policy delta.

Fresh I134 terra-medium returned PASS. It reproduced process controls
`10/10 + 6/6`, static `20/20`, structured serialization `1/1`, in-memory
compile `2/2`, Ruff/format, exact-eight and `217 passed in 1.92s`, with all
four manifests exact before and after. Two private synthetic-harness setup
errors, a callback signature and then a positional drift-token constructor,
were corrected under root-owned DIAG after exact frozen rebinds. Neither
executed a failing candidate path, changed a byte or started a process.

Controller preflight then independently rebound the exact artifact,
controller, probe, all four manifests, exact-two root, equal HEAD/upstream,
empty index, absent invocation and zero related processes.

The one A11-authorized command was:

```text
/usr/bin/python3 -B /Users/wangtao/Documents/DevProject/vibecad-c02-evidence/MRG1-G1-C02-V05/v05_controller.py
```

It exited 1 while evaluating the first `@dataclass(..., slots=True)` decorator,
before entering `main()`:

```text
TypeError: dataclass() got an unexpected keyword argument 'slots'
```

The selected absolute interpreter is Apple `/usr/bin/python3` version 3.9.6.
The environment's normal `python3` resolves to `/usr/local/bin/python3`
version 3.14.2, and the repository venv uses Python 3.13.14. The failure owner
is the root-owned launch adapter's interpreter selection, not product,
FreeCAD, daemon, V05 process policy or the dual gates.

No durable invocation, evidence directory, fixture client, daemon, FreeCAD,
GUI, probe, `Popen` or signal action occurred. `-B` also prevented pycache.
V05 remains exact-two with the same hashes and manifest, related live
processes are zero, HEAD/upstream remain `0e1d2b0…`, and the index remains
empty.

A11-D06 makes this command terminal regardless of whether `main()` began.
V05 will not be retried.

### 97.75 Proposed authorization MRG1-G1-C02-A12

`MRG1-G1-C02-A12` would authorize exactly one successor
`MRG1-G1-C02-V06` and only correction of the root-owned launch adapter:

1. **A12-D01 — new identity.** Create one owner-private V06 root, initially
   exact-two `v06_controller.py` and `v06_probe.py`, with the same ownership,
   mode and link constraints.
2. **A12-D02 — frozen scope.** Preserve V02R, V03, V04, V05 and product
   exact-eight byte-for-byte. Mechanically re-namespace V05 to V06; aside from
   namespace and current artifact identity, controller and probe must
   normalize to V05 with zero residual. No product, API, protocol, runtime,
   FreeCAD UI or process-policy change is authorized.
3. **A12-D03 — interpreter admission.** Bind the final command to the
   canonical resolved identity of `/usr/local/bin/python3`, require
   Python `>=3.10`, and execute one import-safe decorator compatibility check
   against the exact V06 controller before launch. Do not use
   `/usr/bin/python3`.
4. **A12-D04 — proportionate gate.** Reproduce exact-two, normalized-zero
   controller/probe, artifact literal, compile, Ruff/format, exact-eight,
   `217 passed`, prior manifests, process zero and invocation absence. One
   terra-medium mechanical admission is sufficient; no repeated sol-max
   architecture review is required because no policy logic changes.
5. **A12-D05 — one invocation, zero retry.** Only mechanical GREEN permits one
   V06 controller command through the admitted absolute interpreter. Success
   or failure is terminal; no automatic V07.
6. **A12-D06 — success-only closeout.** V06 exit 0, screenshot inspection and
   compliant cleanup admit exact C02 staging, tests, commit and push. Failure
   preserves evidence and returns to the user.

Approval wording:

> Approve `MRG1-G1-C02-A12` as recorded in the current artifact revision:
> authorize one mechanically identical V06 successor, bind launch to the
> admitted `/usr/local/bin/python3 >=3.10`, use one proportionate mechanical
> gate and one zero-retry invocation; forbid automatic V07.

No V06 file, mutation, process or invocation is authorized until the user
explicitly approves A12.

#### MRG1-S96

1. **Completed milestones:** I133 GO `0/0/0/0` and I134 PASS admitted exact
   V05; its unique command failed pre-main because the root adapter selected
   Python 3.9.6; no product process or durable invocation exists.
2. **Next steps:** obtain explicit A12 approval. If approved, create the
   mechanically identical V06, bind and import-check the Python 3.14.2
   interpreter, run one mechanical admission and then one terminal command.
3. **Active decisions:** A11/V05 is terminal. A12-D01..D06 are proposed only;
   V02R/V03/V04/V05/product are immutable and no V06/V07 is authorized.
4. **Execution discipline:** artifact-only approval gate; no V05 retry, no
   policy or product change, and no stage/commit/push before a successful
   authorized successor.

| Entry ID | Decision / approval | Commit / push | Gate evidence | Residual | Snapshot | State |
|---|---|---|---|---|---|---|
| MRG1-G1-C02-E74 | A11 terminal V05; A12 proposed | `not-created`; forbidden | I133 GO `0/0/0/0`; I134 PASS; V05 exit1 pre-main; process/Popen0; exact-two frozen | GUI/plugin/review screenshot and compliant cleanup unproven; A12 not approved | MRG1-S96 | terminal V05 failure / A12 approval gate open |

## 98. MRG1-R3 lean FreeCAD G1 closeout proposal

### 98.1 Gate-density audit and supersession

At `2026-07-31T03:23:41Z` the controller paused execution at the user's
request and audited the orchestration itself. No V06, test, FreeCAD process,
source mutation, staging or commit was started by this audit.

The audit found that V05 applied approximately three validation layers to the
same frozen candidate:

```text
affected 217-test suite              implementer + controller + mechanical
process RED/GREEN/negative matrix    implementer + deep review + mechanical
Ruff/format/compile                  implementer + controller + mechanical
hash/manifest rebind                 repeated after each setup-only deviation
```

The 217-test suite itself takes about two seconds; the dominant cost was
repeated agent reasoning, privately reconstructed harnesses, gate restarts and
rolling-artifact hash rebinding. This density did not prevent the simpler
launch-adapter mistake: `/usr/bin/python3` 3.9.6 was selected without first
executing the exact controller import.

The governing Skill requires the lightest gate that proves the affected risk.
The old plan exceeded that requirement. Every gate below therefore has one
unique evidence contribution; a gate that proves the same proposition as an
earlier gate is prohibited.

Proposed `MRG1-G1-C02-A12` in Section 97.75 was never approved. R3 marks it
`superseded-unapproved`. In particular, R3 removes:

- a new Vxx evidence root for every pre-main/setup failure;
- controller binding to the mutable rolling-artifact hash;
- full C02 tests and dual agent admission on mechanically identical bytes;
- a user approval after every product-neutral harness or environment
  correction.

V02R, V03, V04 and V05 remain immutable historical evidence. They are not
deleted, modified, retried or used as a positive real-GUI result.

### 98.2 Context and objective

The current anchor is branch `codex/agent-stage3` at equal local/upstream
`0e1d2b0db9523685dacbdb5261ac87a14c1dfbf6`, with an empty index. G1-C01 and
its real M00 gate are committed and pushed. C02 has frozen exact-eight product
and test bytes but lacks real preview acceptance, screenshot, cleanup,
commit and push. C03, C04, deterministic packaged launch and the integrated
FreeCAD flow remain open.

R3 has one product objective:

> deliver a source-installable FreeCAD G1 alpha that needs no system FreeCAD,
> launches the packaged VibeCAD addon through the managed runtime, opens safe
> HEAD/draft previews, performs authoritative Accept/Reject, captures supported
> whole-object/feature selectors, survives the named restart paths and cleans
> up its isolated process/runtime state.

This is not durable beta, Addon Manager publication, release/tag creation,
MR1 writer activation or multi-CAD product support. The second CAD remains
architecture-only. MR1-P02/P03 are paused until FreeCAD G1 alpha closeout.

### 98.3 Proposed decision MRG1-G1-A13

`MRG1-G1-A13` is the only planned approval point. If approved, it activates:

1. **A13-D01 — one campaign authorization.** Record and push one artifact-only
   approval commit, then autonomously execute the complete sequence in
   Section 98.4. Do not request another planned approval for an already named
   gate, technical correction, commit or push.
2. **A13-D02 — existing product contracts.** Preserve A04's C02, C03 and C04
   product behavior and exact public authority boundaries. No protocol, tool,
   operation, durable schema or shared daemon/Application seam changes.
3. **A13-D03 — deterministic managed launch.** Add `vibecad --freecad`.
   It uses the existing managed runtime installer/status/receipt and existing
   GUI path helper, packages the exact `freecad/VibeCAD` addon, validates the
   resolved interpreter/runtime/addon/GUI identities and launches without
   reading or writing a system FreeCAD installation or normal user `Mod`
   directory. No new dependency is introduced.
4. **A13-D04 — versionless acceptance runner.** Replace Vxx cloning with one
   reusable owner-private runner root:

   ```text
   /Users/wangtao/Documents/DevProject/vibecad-c02-evidence/MRG1-G1-ACCEPTANCE
     authorization.json
     runner.py
     probe.py
     runs/<unique-run-id>/
   ```

   The immutable authorization manifest binds the pushed A13 approval commit,
   exact candidate hashes, interpreter realpath/stat/hash/version, managed
   runtime receipt/generation, GUI identity and addon identity. The runner
   never binds the mutable rolling artifact. Current host sandbox/permission
   rules remain authoritative; user approval does not bypass a required
   filesystem permission prompt.
5. **A13-D05 — rational consumption boundary.** Interpreter/import/manifest
   or adapter failures before durable invocation and before FreeCAD GUI
   `Popen` do not consume a semantic GUI attempt. At most two bounded
   preflight corrections are permitted. Once GUI `Popen` occurs or product
   state can change, never blind-replay. An unknown Accept/Reject outcome is
   resolved only by durable reread.
6. **A13-D06 — technical autonomy.** A genuine in-scope defect may be fixed
   inside the current exact allowlist and remaining correction budget without
   another user approval. Setup-only gate errors resume from the last proven
   checkpoint and do not restart unchanged gates. Scope, authority, product
   shape or irreversible-action expansion remains a breaker.
7. **A13-D07 — evidence separation.** Raw command output, screenshots and
   process evidence live in the owner-private runner root. The repo artifact
   stores only compact hashes, counts, verdicts, residuals and recovery
   anchors. Appending the ledger cannot invalidate a frozen runner.
8. **A13-D08 — execution routing.** Routine coding uses
   `gpt-5.6-sol / high`; mechanical integrity uses
   `gpt-5.6-terra / medium`; `gpt-5.6-sol / max` is used only for new
   architecture/authority boundaries, adversarial findings or the first
   semantic admission of the acceptance runner that controls real GUI
   execution. Shared Workbench writers are serial. Only read-only review,
   gate execution on a frozen candidate and next-stage research may overlap.
9. **A13-D09 — consolidated real evidence.** C02 retains one real preview
   gate before its commit. C03 and C04 use focused RED/GREEN plus independent
   semantic review for their individually revertible commits; their former
   V02/V03/V04 real-GUI scenarios are consolidated without omission into the
   installed-form `REAL-G1` before the integration commit. A real finding is
   corrected in the owning allowlist and consumes the bounded corrective
   commit/run budget. This explicitly supersedes A04's per-C03/per-C04 real
   gate timing, not their required observations.

### 98.4 Exact commit sequence

Normal budget is six commits. The hard ceiling is eight, allowing at most two
evidence-backed corrective commits without expanding an allowlist or product
contract.

| ID | Subject | Outcome | Independent gate |
|---|---|---|---|
| A13-PERSIST | `docs(orchestration): authorize FreeCAD G1 closeout` | persist R3 and exact user approval; establish immutable approval anchor | `AUTH-G0` |
| G1-C02 | `feat(workbench): preview managed head and draft` | close frozen C02 through real preview acceptance and cleanup | inherited C02 evidence + `REVIEW-RUNNER` + `ENV-PREFLIGHT` + `REAL-C02` + `STAGE-G0-C02` |
| G1-C03 | `feat(workbench): accept or reject reviewed draft` | fresh authority, durable reread and no blind replay | `FOCUSED-C03` + `REVIEW-C03` + `STAGE-G0-C03` |
| G1-C04 | `feat(workbench): capture managed object selectors` | whole-object/feature selectors; subelement/ambiguity fail closed | `FOCUSED-C04` + `REVIEW-C04` + `STAGE-G0-C04` |
| G1-INTEGRATION | `feat(freecad): add deterministic G1 launch and acceptance` | packaged addon, `vibecad --freecad`, installed-form integrated real E2E | `FOCUSED-INTEGRATION` + `REVIEW-INTEGRATION` + `REAL-G1` + `FULL-1` + stage G0 |
| G1-CLOSE | `docs(orchestration): close FreeCAD G1 alpha` | truthful EN/ZH usage, evidence, residuals and pushed recovery anchor | `CLOSE-G0` |

Every commit is staged by exact named paths, committed and pushed before the
next writer begins. No broad staging command is permitted.

### 98.5 Exact file allowlists

#### A13-PERSIST

```text
M docs/orchestrated/vibecad-multi-runtime-g1.md
```

#### G1-C02

```text
M docs/orchestrated/vibecad-multi-runtime-g1.md
M freecad/VibeCAD/vibecad_workbench/state.py
M freecad/VibeCAD/vibecad_workbench/gateway.py
M freecad/VibeCAD/vibecad_workbench/dock.py
M freecad/VibeCAD/vibecad_workbench/host.py
A freecad/VibeCAD/vibecad_workbench/preview.py
M tests/fixtures/freecad_workbench/fake_host.py
M tests/test_freecad_workbench_controller.py
A tests/test_freecad_workbench_preview.py
```

#### G1-C03

```text
M docs/orchestrated/vibecad-multi-runtime-g1.md
M freecad/VibeCAD/vibecad_workbench/state.py
M freecad/VibeCAD/vibecad_workbench/gateway.py
M freecad/VibeCAD/vibecad_workbench/dock.py
M freecad/VibeCAD/vibecad_workbench/preview.py
M tests/fixtures/freecad_workbench/fake_host.py
M tests/test_freecad_workbench_controller.py
M tests/test_freecad_workbench_preview.py
A tests/test_freecad_workbench_review.py
```

#### G1-C04

```text
M docs/orchestrated/vibecad-multi-runtime-g1.md
M freecad/VibeCAD/vibecad_workbench/state.py
M freecad/VibeCAD/vibecad_workbench/dock.py
M freecad/VibeCAD/vibecad_workbench/host.py
A freecad/VibeCAD/vibecad_workbench/selection.py
M tests/fixtures/freecad_workbench/fake_host.py
M tests/test_freecad_workbench_controller.py
A tests/test_freecad_workbench_selection.py
```

#### G1-INTEGRATION

```text
M docs/orchestrated/vibecad-multi-runtime-g1.md
M pyproject.toml
M README.md
M README.zh-CN.md
M src/vibecad/launcher.py
A src/vibecad/freecad_launcher.py
M tests/test_launcher.py
A tests/test_freecad_launcher.py
M tests/test_freecad_workbench_package.py
A tests/fixtures/freecad_workbench/g1_e2e_harness.py
A tests/test_freecad_workbench_e2e.py
```

`uv.lock` is excluded because dependencies and versions do not change.
`manifest.json` and `mcpb_entry.py` are excluded because the MCP 28-tool
surface and entrypoint do not change. Runtime paths/status/installer and the
C01 GUI harness remain read-only; existing verified interfaces are reused.

The installed wheel/sdist must include the exact addon sources. The launcher
uses only an owner-controlled packaged addon path and the verified managed
FreeCAD GUI. It may invoke the existing installer when the managed runtime is
absent, but may not add a downloader, use a system FreeCAD, write a normal user
`Mod` directory or silently fall back to an unverified binary.

#### G1-CLOSE

```text
M docs/orchestrated/vibecad-multi-runtime-g1.md
M README.md
M README.zh-CN.md
M docs/ARCHITECTURE.md
M docs/ACCEPTANCE_TESTS.md
```

Only files whose statements need final evidence reconciliation are modified.

### 98.6 Reviewed gate content

Each gate has one and only one evidence question:

| Gate | Question proved | Explicit non-purpose |
|---|---|---|
| `AUTH-G0` | Is the approved revision, allowlist, exclusion set, branch anchor and budget exact? | no behavior test |
| `FOCUSED-*` | Does the new behavior fail for the intended reason and then pass on the smallest candidate? | no full-suite claim |
| `REVIEW-RUNNER` | Do the exact manifest-bound runner/probe assertions prove identity, single execution, fail-closed authority, evidence durability and cleanup before the first GUI `Popen`? | no product PASS and no replay of inherited C02 tests |
| `REVIEW-*` | Do the settled production and test diffs preserve authority, contain non-vacuous contract assertions and fail closed against the named attacks? | reviewer inspects test source and RED/GREEN evidence but does not rerun pytest/Ruff |
| `STAGE-G0-*` | Are the cached named paths and blobs exactly the reviewed candidate? | no semantic retest |
| `ENV-PREFLIGHT` | Are interpreter, exact import, authorization manifest, managed receipt/generation, GUI/addon identity and cold process state executable now? | no product PASS |
| `REAL-C02` | Do distinct HEAD/draft documents, checkout/grant identity, screenshot and document→checkout→client cleanup work in real FreeCAD? | no C03/C04 claim |
| `REAL-G1` | Does the packaged installed form execute preview, Accept, Reject, selector, restart and cleanup end to end? | no durable-beta/release claim |
| `FULL-1` | Does the final integrated candidate preserve the complete non-slow repository baseline? | run once, not per commit |
| `PUSH/CLOSE-G0` | Are the exact commits upstream, the index empty, release workflow untriggered and ledger truthful? | no behavior duplication |

C02 inherits the frozen exact-eight hashes, I132 RED/GREEN, I133
`0/0/0/0`, I134 PASS and the three completed `217 passed` results. Mechanically
identical runner work does not rerun 217, Ruff, format, exact-34 or the process
matrix. Before the first real launch, one fresh sol-max `REVIEW-RUNNER` reads
the exact final runner, probe and authorization manifest sources and their
bound hashes. It must return PASS before `ENV-PREFLIGHT`; a finding is fixed
and re-reviewed without consuming a GUI attempt. Only then does the runner
perform exact identity/import/environment preflight and the missing real
acceptance.

C03 and C04 each run their focused command once on the final candidate.
Their independent semantic reviewer reads both production and test source,
plus the focused RED/GREEN evidence, but does not rerun or reconstruct the
test harness. `REVIEW-INTEGRATION` applies the same rule to launcher,
packaging, E2E harness and test sources. A mechanical agent is not added
unless the gate's evidence question is otherwise unowned.

Every `REVIEW-RUNNER` and `REVIEW-*` verdict explicitly records:

1. the assertion-to-contract mapping and the genuine pre-fix RED signal;
2. absence of tautological, always-true, exception-swallowing or
   implementation-only assertions;
3. positive, negative, unknown-outcome and cleanup/restart cases required by
   that packet;
4. proof that mocks/fakes cannot bypass the named production boundary;
5. preservation of authority, at-most-once behavior and fail-closed defaults;
6. any deleted or weakened assertion, which is a breaker unless independently
   justified by the approved contract.

A passing command is therefore necessary but not sufficient: the semantic
review of its assertion content must also pass.

The complete non-slow suite runs only on the final integrated installed-form
candidate. A second full run is allowed only if an authorized corrective
change invalidates the first result; never retry an unchanged red.

#### 98.6.1 Exact gate commands

All repository commands run from
`/Users/wangtao/Documents/DevProject/vibecad`. They set
`PYTHONDONTWRITEBYTECODE=1` and disable pytest's cache provider so a read-only
gate does not create an unreviewed repository entry.

`ENV-PREFLIGHT` and the two real gates use the exact admitted interpreter and
versionless runner:

```text
/usr/local/bin/python3 -I -B \
  /Users/wangtao/Documents/DevProject/vibecad-c02-evidence/MRG1-G1-ACCEPTANCE/runner.py \
  --manifest /Users/wangtao/Documents/DevProject/vibecad-c02-evidence/MRG1-G1-ACCEPTANCE/authorization.json \
  --preflight

/usr/local/bin/python3 -I -B \
  /Users/wangtao/Documents/DevProject/vibecad-c02-evidence/MRG1-G1-ACCEPTANCE/runner.py \
  --manifest /Users/wangtao/Documents/DevProject/vibecad-c02-evidence/MRG1-G1-ACCEPTANCE/authorization.json \
  --run c02-preview

/usr/local/bin/python3 -I -B \
  /Users/wangtao/Documents/DevProject/vibecad-c02-evidence/MRG1-G1-ACCEPTANCE/runner.py \
  --manifest /Users/wangtao/Documents/DevProject/vibecad-c02-evidence/MRG1-G1-ACCEPTANCE/authorization.json \
  --run g1-installed-e2e
```

The runner must reject an interpreter whose realpath/stat/hash/version differs
from the manifest before importing candidate code. `-I` must not prevent the
runner from admitting the exact manifest-bound repository and packaged-addon
paths explicitly.

`FOCUSED-C03`:

```text
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -q -p no:cacheprovider \
  tests/test_freecad_workbench_controller.py \
  tests/test_freecad_workbench_preview.py \
  tests/test_freecad_workbench_review.py
.venv/bin/python -m ruff check \
  freecad/VibeCAD/vibecad_workbench/state.py \
  freecad/VibeCAD/vibecad_workbench/gateway.py \
  freecad/VibeCAD/vibecad_workbench/dock.py \
  freecad/VibeCAD/vibecad_workbench/preview.py \
  tests/fixtures/freecad_workbench/fake_host.py \
  tests/test_freecad_workbench_controller.py \
  tests/test_freecad_workbench_preview.py \
  tests/test_freecad_workbench_review.py
.venv/bin/python -m ruff format --check \
  freecad/VibeCAD/vibecad_workbench/state.py \
  freecad/VibeCAD/vibecad_workbench/gateway.py \
  freecad/VibeCAD/vibecad_workbench/dock.py \
  freecad/VibeCAD/vibecad_workbench/preview.py \
  tests/fixtures/freecad_workbench/fake_host.py \
  tests/test_freecad_workbench_controller.py \
  tests/test_freecad_workbench_preview.py \
  tests/test_freecad_workbench_review.py
```

`FOCUSED-C04`:

```text
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -q -p no:cacheprovider \
  tests/test_freecad_workbench_controller.py \
  tests/test_freecad_workbench_selection.py \
  tests/test_object_selectors.py
.venv/bin/python -m ruff check \
  freecad/VibeCAD/vibecad_workbench/state.py \
  freecad/VibeCAD/vibecad_workbench/dock.py \
  freecad/VibeCAD/vibecad_workbench/host.py \
  freecad/VibeCAD/vibecad_workbench/selection.py \
  tests/fixtures/freecad_workbench/fake_host.py \
  tests/test_freecad_workbench_controller.py \
  tests/test_freecad_workbench_selection.py
.venv/bin/python -m ruff format --check \
  freecad/VibeCAD/vibecad_workbench/state.py \
  freecad/VibeCAD/vibecad_workbench/dock.py \
  freecad/VibeCAD/vibecad_workbench/host.py \
  freecad/VibeCAD/vibecad_workbench/selection.py \
  tests/fixtures/freecad_workbench/fake_host.py \
  tests/test_freecad_workbench_controller.py \
  tests/test_freecad_workbench_selection.py
```

`FOCUSED-INTEGRATION`:

```text
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -q -p no:cacheprovider \
  tests/test_launcher.py \
  tests/test_freecad_launcher.py \
  tests/test_freecad_workbench_package.py \
  tests/test_freecad_workbench_e2e.py
.venv/bin/python -m ruff check \
  src/vibecad/launcher.py \
  src/vibecad/freecad_launcher.py \
  tests/test_launcher.py \
  tests/test_freecad_launcher.py \
  tests/test_freecad_workbench_package.py \
  tests/fixtures/freecad_workbench/g1_e2e_harness.py \
  tests/test_freecad_workbench_e2e.py
.venv/bin/python -m ruff format --check \
  src/vibecad/launcher.py \
  src/vibecad/freecad_launcher.py \
  tests/test_launcher.py \
  tests/test_freecad_launcher.py \
  tests/test_freecad_workbench_package.py \
  tests/fixtures/freecad_workbench/g1_e2e_harness.py \
  tests/test_freecad_workbench_e2e.py
```

`FULL-1`:

```text
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -q -p no:cacheprovider
.venv/bin/python -m ruff check src freecad tests
```

Every `STAGE-G0-*` and `CLOSE-G0` runs only:

```text
git diff --cached --check
git diff --cached --name-only
git diff --cached --numstat
git status --short
```

The controller compares the outputs to the commit's exact allowlist, frozen
blob hashes, expected modified/added status, empty unstaged approved scope and
unchanged excluded-name set. These commands are not followed by pytest or a
second semantic review.

### 98.7 Manual and real validation matrix

| Evidence | Scenario | Required observation | Owner |
|---|---|---|---|
| `REAL-C02` | isolated managed FreeCAD opens current project/task | separate clean HEAD and Draft Preview Documents; exact source/checkouts/grants; screenshot; zero cleanup residue | controller |
| `REAL-G1-A` | installed `vibecad --freecad` cold launch | packaged addon discovered exactly once; Dock responsive; managed binary/runtime/addon identities exact | controller |
| `REAL-G1-B` | Accept and Reject across restart | Accept advances HEAD once; Reject leaves HEAD unchanged; unknown outcome resolved by durable reread | controller |
| `REAL-G1-C` | selector capture | whole object and feature entity round-trip uniquely; Face/Edge and ambiguity visibly rejected | controller |
| `REAL-G1-D` | shutdown and retirement | windows/threads/checkouts/grants/client/daemon retire in order; pid/socket/run-root residue zero | controller |
| `G1-M01` | final normal-scale product review | readable Dock, previews, verdict state and copied selector shown in final screenshot set | user review of evidence; no live presence required |

No real user project root, normal FreeCAD profile or system FreeCAD
installation may be touched.

### 98.8 Budget, recovery and circuit breakers

```text
normal commits                    6
hard commit ceiling               8
normal real FreeCAD GUI runs      2  (REAL-C02, REAL-G1)
hard real GUI ceiling             3  (one evidence-backed correction only)
pre-Popen preflight corrections   2 maximum
normal full non-slow suites       1
hard full-suite ceiling           2  (changed corrective candidate only)
normal independent reviews        4  (runner, C03, C04, integration)
conditional corrective reviews   2 maximum
```

An unpredicted candidate RED or review Blocker/Major stops that packet, but an
in-scope correction proceeds autonomously while budget remains. Setup-only
test code or callback errors are not candidate REDs; correct the private
harness and resume only unproved checks.

Execution returns to the user only for one of these breakers:

- a new public API/protocol/tool/operation/durable schema/shared-seam or
  product-shape decision outside A13;
- second-CAD implementation, MR1 activation/non-disposable beta, Addon
  Manager, tag/release/PR, external credentials or spend;
- an unnamed/out-of-allowlist/excluded-path write, destructive user-data
  action or normal user FreeCAD-root access;
- commit 9, GUI run 4, full-suite run 3 or exhausted correction budget;
- branch/upstream/index ambiguity, rejected push or unexpected release run;
- interpreter/runtime/addon/candidate identity drift that cannot be resolved
  inside two preflight corrections;
- duplicate execution after GUI `Popen`, ambiguous process/signal identity,
  cleanup residue or incomplete screenshot/document evidence;
- unknown Accept/Reject outcome that durable reread cannot determine.

All other product-neutral implementation, gate, staging, commit and push work
inside the named plan proceeds without another planned approval.

### 98.9 Expected impact and residuals

Expected product impact:

- public MCP remains exactly 28 tools and six operations;
- Task, Revision, checkout, grant, Accept/Reject and SelectorV1 wire contracts
  remain unchanged;
- no durable writer/schema changes;
- the packaged product gains one explicit local entrypoint,
  `vibecad --freecad`, backed only by the existing managed runtime;
- G1 ends as FreeCAD alpha with repository and installed-form E2E evidence.

Expected testing impact:

- existing C02 evidence is inherited by exact hash;
- the versionless acceptance runner receives one source-and-assertion review
  before its first GUI launch without replaying the inherited C02 suite;
- C03/C04 focused counts increase only with their new tests;
- packaging/launcher tests validate wheel/sdist addon inclusion and fail-closed
  launch selection;
- one final non-slow suite proves cross-component regression.

Residuals after this plan:

| Residual | Disposition | Closure condition |
|---|---|---|
| MR1-P02/P03 and durable-v2 activation | deferred; no parallel writer during G1 closeout | new post-G1 campaign |
| real second CAD | deferred; MR0 adapter/conformance reservation only | explicit product demand and new approval |
| non-disposable beta/migration | prohibited claim | shared beta gate from Section 28.7 |
| Addon Manager/public release/tag | out of scope | separate release plan and approval |
| external-drive evidence archive | preferred but no writable external volume observed | mount and authorize a writable evidence destination; current owner-private out-of-repo root remains valid |
| `.workbuddy/**` and named course documents | user-owned exclusion | never read, edit, stage or package |

### 98.10 Approval wording and execution hold

Approval wording:

> Approve `MRG1-G1-A13` as recorded in the current artifact revision:
> supersede unapproved A12; authorize the six-commit lean FreeCAD G1 closeout,
> reusable immutable-manifest acceptance runner, explicit runner and unit-test
> assertion-source review, reviewed unique-purpose gate matrix, bounded
> product-neutral recovery, exact file allowlists, deterministic packaged
> `vibecad --freecad` launch and integrated G1 alpha acceptance;
> pause MR1-P02/P03 and second-CAD implementation; require a new approval only
> on the recorded breaker conditions.

No A13 implementation, test, runner creation, FreeCAD process, staging,
commit or push is authorized until the user explicitly approves the final R3
artifact revision.

#### MRG1-S97

1. **Completed milestones:** gate-density audit, current-state inventory,
   bounded deep orchestration review and exact launcher/integration allowlist
   design are complete; A12 is superseded-unapproved; V05 and all product
   bytes remain frozen.
2. **Next steps:** run only document-plan consistency checks, show the final
   R3 revision and A13 authorization wording, and wait. After explicit A13,
   persist the approval commit and execute Section 98.4 without planned
   intermediate approval.
3. **Approved decisions:** A01–A11 historical approvals remain append-only;
   A04 product boundaries remain active; A12 was never approved; A13 is
   proposed only. No V06 or implementation action is currently authorized.
4. **Execution discipline:** `native-plan`, `spawn-send-wait`,
   `repo-artifact`, `native-session-poll`, Codex adapter. Current public host
   configuration is workspace-write with external paths permission-controlled;
   the Skill/artifact cannot expand it. Excluded user paths remain unread,
   unmodified and unstaged.

Required capability evidence categories for the proposed execution profile:

- `live capability declarations`: native plan projection, bounded command
  sessions and spawn/send/follow-up/wait collaboration are declared.
- `observable behavior`: this campaign has observably updated native plans,
  delegated bounded tasks and polled original native sessions.
- `environment identity`: Codex desktop on the local macOS workspace
  `/Users/wangtao/Documents/DevProject/vibecad`.
- `public configuration`: workspace-write repository access, restricted
  network and permission-controlled external writes; none expands A13.

| Entry ID | Decision / approval | Commit / push | Gate evidence | Residual | Snapshot | State |
|---|---|---|---|---|---|---|
| MRG1-R3-E01 | A12 superseded-unapproved; A13 proposed | `not-created`; forbidden | density audit; mechanical inventory; deep plan review; exact integration allowlist | C02 real acceptance and C03/C04/integration/closeout open | MRG1-S97 | plan generated / consistency review then approval hold |

## 99. MRG1-G1-A13 authorization activation

At `2026-07-30 23:26:03 PDT` (`2026-07-31T06:26:03Z`), the user explicitly
approved the exact Section 98 revision whose pre-authorization artifact
SHA-256 was:

```text
24daa8c9e15831e755f17ab6c222ce1a9eaff3e603f2b6221467b259025cd9da
```

The user's exact authorization words were:

> 批准 MRG1-G1-A13

This activates `MRG1-G1-A13-D01..D09`, the six-commit sequence, exact
allowlists, unique-purpose gates, budgets, residual dispositions and breaker
conditions in Section 98. It supersedes only the unapproved A12 proposal.
A04's product contracts remain active. The approval does not grant host
permissions, does not authorize excluded paths and does not authorize MR1,
second-CAD implementation, release, tag or PR work.

The execution profile is revalidated without expansion:

```text
approval: native-plan
delegation: spawn-send-wait
persistence: repo-artifact
process: native-session-poll
adapter: Codex
```

Required capability evidence categories:

- `live capability declarations`: `update_plan`, bounded command sessions,
  and spawn/send/follow-up/wait collaboration are declared live in this
  session.
- `observable behavior`: the native plan projection was updated, the approval
  revision was read back exactly and prior bounded native sessions were
  polled without duplicate launch.
- `environment identity`: Codex desktop on the local macOS workspace
  `/Users/wangtao/Documents/DevProject/vibecad`.
- `public configuration`: repository workspace-write access, restricted
  network and permission-controlled external writes; none is expanded by
  A13.

### MRG1-S98

1. **Completed milestones:** A13 was approved against exact artifact SHA-256
   `24daa8c…`; branch and upstream are both `0e1d2b0…`; the index is empty.
2. **Next steps:** persist and push `A13-PERSIST`; then admit the versionless
   runner, execute `REAL-C02` once, close C02 and continue the approved
   sequence.
3. **Approved decisions:** A13-D01..D09 at the recorded revision and exact
   user wording; A04 product contracts remain active; A12 remains
   superseded-unapproved.
4. **Execution discipline:** `native-plan`, `spawn-send-wait`,
   `repo-artifact`, `native-session-poll`, Codex adapter; exact allowlists,
   unique-purpose gates and Section 98.8 breakers apply; excluded user paths
   remain unread, unmodified and unstaged.

| Entry ID | Decision / approval | Commit / push | Gate evidence | Residual | Snapshot | State |
|---|---|---|---|---|---|---|
| MRG1-G1-A13-E01 | A13-D01..D09; user exact words above | `A13-PERSIST` pending self-anchor | `AUTH-G0`: approval SHA, branch/upstream, index, allowlist and budget exact | C02 real acceptance and later G1 packets open by plan | MRG1-S98 | authorized / persistence gate active |
