# VibeCAD Active Plan — P2 mechanical delivery

> Status: **P2-S01 through P2-S04 implemented and verified; publication pending**
>
> Updated: 2026-08-02
>
> Repository anchor: `codex/agent-stage3@5a1bb43` plus the verified P2-S04
> publication candidate described below
>
> This is the only mutable orchestration plan. P1 sequential editing closed at
> `7f3d506`; earlier campaign files remain historical records.

## 1. Current product truth

The completed P1 campaign delivered the sequential interaction slice:

```text
Agent candidate -> user review -> Accept/Reject
-> optional user FreeCAD edit -> explicit checkpoint -> new Revision/HEAD
```

That completion does **not** mean every capability listed under the broad P1
roadmap is implemented. Sketcher, broad PartDesign, Selector Level B, controlled
STEP/STL import, and mesh-to-faceted-BRep remain future work. Product documents
must refer to the completed milestone as **P1 sequential editing / G2**, not as
completion of the entire historical P1 capability inventory.

The public semantic operation registry now contains nine operations. The six
existing direct operations remain direct-exposed. Three ModelProgram-only
operations, `create_component`, `place_component`, and `set_component_bom`,
establish the bounded assembly and flat-BOM path; `create_box` and
`create_cylinder` accept an optional explicit component target. The MCP surface
contains 31 tools: the prior 28 plus `create_release`, `get_release`, and
`approve_release`.

The repository also contains a broader legacy Round-8 assembly implementation
with real FreeCAD evidence for:

- `App::Part` multi-part containment;
- per-part feature chains and rigid placement;
- planar face alignment;
- pairwise interference detection;
- assembly-aware description, rendering, and STEP export.

P2-S01/S02 migrated only explicit containment, absolute rigid placement, and
pairwise interference through ModelProgram, candidate verification, Revision,
review, and HEAD CAS. Legacy active-part selection, planar alignment, rendering,
and other server semantics remain private implementation evidence rather than an
alternative write authority or claimed product capability.

## 2. Recommended P2 product boundary

**P2-A: rigid multi-part delivery MVP** is now complete. It is the smallest slice
that turns proven internal assembly mechanics into a trustworthy product outcome.

P2-A product story:

```text
Agent creates or edits explicit components in one isolated FCStd candidate
-> applies deterministic rigid placements
-> observes per-component geometry and assembly interference
-> user reviews the assembly in the Workbench
-> Accept publishes one immutable assembly Revision with FCStd and STEP
```

Initial limits:

- one assembly is one managed FCStd candidate and one authoritative Revision;
- 2–10 `App::Part` components;
- every component has an explicit stable component identity; no hidden
  `active_part` or ambient component selection is allowed in public execution;
- component creation and geometry operations bind the target component
  explicitly, including through typed ResultRef where appropriate;
- first placement is absolute rigid pose; planar alignment is admitted only
  after its component/connector selector is stable across reload;
- deterministic pairwise interference is reported and fails closed when the
  product claim requires an interference-free result;
- observation and preservation are per component and for the whole assembly;
- accepted output remains the existing FCStd plus STEP pair.

This boundary deliberately does not claim FreeCAD Assembly solver semantics.

## 3. Deferred from P2-A

- native Assembly joints/mates, DOF solving, motion, animation, and explosion;
- external linked component revisions, cross-project instances, subassemblies,
  configurations, or branching/merge;
- semantic face/edge connectors before Selector Level B is ready;
- automatic conflict resolution or simultaneous Agent/user editing;
- broad Sketcher/PartDesign/import work merely to make assembly demos richer;
- structured BOM/PLM, editable TechDraw pages, manufacturing drawings, GD&T,
  and multi-sheet/detail/section drawings; P2-S03/S04 deliver only the bounded
  flat BOM, deterministic assembly PDF, immutable approval, and delivery ZIP;
- a second CAD backend, durable schema migration, release/tag, or PR.

Native joints and the broader modeling inventory remain future product work;
they are not implicit follow-ons to the completed P2 delivery slice.

## 4. Execution slices

### P2-S01 — Explicit component and observation contract

State: **complete at the current candidate boundary**.

- define stable component identity for `App::Part` within one Revision;
- extend observation with deterministic per-component structure, placement,
  geometry facts, and assembly summary;
- define the strict component-selector predicate that future public operations
  must use without expanding the current public operation surface;
- keep the legacy Session implementation private and reuse only bounded,
  independently testable mechanics;
- reject ambiguous, duplicate, missing, stale, or cross-revision component
  references before CAD effect.

Gate: focused registry/program/selector/observation tests plus one real FreeCAD
reload proving component identity and placement survive FCStd save/reopen.

Evidence on 2026-08-02:

- `ComponentObservation` binds stable component ID, provenance, placement,
  member IDs, and global geometry while preserving historical snapshot digests
  when no components are present;
- `Session.create_component` creates an identified `App::Part` without implicit
  migration, and strict component records reject unidentified/duplicate/nested
  membership;
- in-process and Worker observation paths compare live and reloaded components;
- real FreeCAD save/close/reload preserved two component IDs, member IDs,
  placements, global centers, aggregate volume, and aggregate bounding box;
- real managed Worker observation passed;
- full non-slow suite: `5577 passed, 111 deselected`; full Ruff gate passed.

### P2-S02 — Rigid placement and interference vertical slice

State: **complete at the current candidate boundary**.

- add minimal component creation and explicit-target primitive operations;
- add deterministic absolute component placement;
- calculate pairwise interference from reloaded global shapes;
- verify per-component preservation, total assembly geometry, and requested
  interference policy;
- deliver one accepted two-component FCStd/STEP through the existing review path.

Gate: focused Task Kernel integration plus one real managed FreeCAD outcome.
No new test controller or validation framework is admitted.

Evidence on 2026-08-02:

- the registry adds only `create_component` and `place_component`; both are
  ModelProgram-only, while existing primitive creation gains an optional
  explicit component target and the direct tool count remains unchanged;
- absolute component placement preserves stable component/member identity and
  rejects an interfering final pose transactionally;
- sealed observations contain a deterministic complete pairwise common-volume
  matrix, preserved across the in-process and Worker reload boundaries;
- assembly acceptance supports bounded `component_count` and the explicit
  `interference_free=true` product claim;
- a real Task Kernel run created two components, exported FCStd/STEP, passed all
  12 acceptance criteria, published a review draft, accepted it, and advanced
  Revision/HEAD;
- a separate real managed Worker run preserved shape, four entity records, two
  component records, and the non-interfering pair across checkpoint, STEP
  export, close, and FCStd reload;
- the real in-process FreeCAD program independently passed checkpoint/reload and
  STEP geometry checks;
- full non-slow suite: `5583 passed, 114 deselected`; full Ruff and changed-file
  format gates passed.

### P2-S03 — Flat BOM

State: **complete at the current candidate boundary**.

- add bounded part number, description, material/density, and quantity metadata;
- derive a flat BOM from sealed component facts;
- bind BOM to the exact accepted Revision and include machine-readable output;
- do not claim where-used, external part master, configurations, or PLM.

Evidence on 2026-08-02:

- `set_component_bom` persists canonical, bounded component metadata in the
  FCStd itself without expanding the direct public tool surface;
- quantity is derived from explicit component count; components sharing a part
  number aggregate only when metadata and local-geometry digest match exactly,
  otherwise the sealed observation reports a conflict and the BOM is incomplete;
- unit and total mass are derived from sealed component volume and density;
- canonical JSON and CSV are bound to the candidate Revision in operation output
  and sealed task evidence; creating physical BOM files in a downloadable release
  package remains P2-S04 work;
- assembly acceptance supports `bom_complete`, `bom_row_count`,
  `bom_total_quantity`, and tolerant `bom_total_mass` in kilograms;
- a real Task Kernel run created two identical components, aggregated quantity
  two, passed 16 acceptance criteria, published review, accepted it, and advanced
  Revision/HEAD; a separate real managed Worker checkpoint/reload run preserved
  the same BOM facts;
- full non-slow suite: `5593 passed, 114 deselected, 19 warnings`; full Ruff,
  changed-file format, and diff-integrity gates passed.

### P2-S04 — TechDraw and release package

State: **implemented and verified at the publication candidate boundary**.

- produce a bounded drawing template with assembly views, balloons, and revision
  identity;
- package verified FCStd, STEP, flat BOM, drawing export, manifest, and validation
  report;
- distinguish an immutable VibeCAD Revision from a later release approval state.

Approved minimum boundary on 2026-08-02:

- generate one deterministic A3 landscape assembly PDF from TechDraw HLR with
  front, right, top, and isometric views, a flat-BOM table, stable item numbers,
  one representative balloon per BOM row, and exact Revision identity;
- create one immutable delivery ZIP containing FCStd, STEP, BOM JSON/CSV,
  assembly PDF, manifest, and the sealed validation report;
- build only from an accepted Revision, review the exact package digest, and
  record Release approval separately without changing the Revision or HEAD;
- deliver generation, preview, approval, and download through the existing
  application/Workbench boundary without a second generic workflow engine;
- exclude native editable TechDraw pages, manufacturing drawings, GD&T,
  multi-sheet/detail/section views, native joints, structured BOM/PLM, STL,
  cloud upload, and automatic external publication from the S04 implementation.

Native joints/DOF solving can be evaluated only after this rigid-delivery path
is stable; it is not silently included in P2-S04.

Evidence on 2026-08-02:

- one accepted Revision produces an A3 landscape assembly PDF with deterministic
  HLR front/right/top/isometric projections, title block, flat-BOM rows, stable
  item numbers, representative balloons, and exact Revision identity;
- one immutable Release draft seals FCStd, STEP, BOM JSON/CSV, drawing PDF,
  manifest, and validation report into an exact seven-entry ZIP; approval binds
  the reviewed SHA-256 digest without mutating Revision or HEAD;
- Release source and package reads fail closed on replacement, symlink, mode,
  owner, link-count, metadata-instability, or digest mismatch, and the buffered
  MCP Resource boundary rejects artifacts or packages above 64 MiB;
- application, daemon, 31-tool MCP surface, ResourceLink/resources-read Blob,
  supervisor replay, and Workbench create/preview/approve/download paths share
  the same Release authority and idempotency semantics;
- the real managed FreeCAD Task Kernel flow created and accepted a two-component
  interference-free assembly, generated the PDF, approved the exact package,
  and verified the seven ZIP entries plus FCStd/STEP hashes after reload;
- final full suite: `5622 passed, 114 deselected, 19 warnings`; the real FreeCAD
  Release gate independently passed; Ruff, changed-file format, source compile,
  and diff-integrity gates passed.
- the `v0.6.0` version guard, wheel/sdist metadata, clean MCPB allowlist,
  deterministic Skill archive, cross-channel source parity, and fresh Python
  3.12 wheel/sdist installs passed locally; the exact packed MCPB managed-runtime
  gate remains delegated to tag CI because the installed local runtime was not
  mutated to the candidate receipt, and both external publishers depend on that
  gate.

## 5. Authority, controls, and gates

Active product invariants remain unchanged:

- Task Kernel is the sole Task, candidate, verifier, Revision, review, HEAD, and
  recovery authority;
- Workbench is an interaction client, not a second writer;
- user and Agent ownership is sequential; no live merge or background sync;
- Git is an optional mirror/delivery boundary, not CAD authority;
- managed FreeCAD remains the only connected CAD adapter;
- unrelated `.workbuddy/` and the two CAD course documents remain untouched.

Selected controls are intentionally small:

- **Continuity:** keep this compact current plan because P2 crosses sessions.
- **Approval:** confirm the next product boundary before changing public component
  semantics or broadening the accepted model envelope.
- **Gates:** G1 focused contracts for S01; G2 Task Kernel integration for S02;
  G2 Task Kernel plus real managed FreeCAD revision/reload evidence for S03;
  full repository, real FreeCAD Release, MCP/resource, and Workbench gates for
  S04.

No delegation, independent-review ceremony, production ledger, background
controller, new validation framework, PR, release, or deployment is selected.
The user's standing publication instruction permits intentional commits and
branch pushes when a coherent verified slice is ready; scope must still be
audited because the worktree contains unrelated untracked paths.

## 6. Decision and next action

The user approved the following product boundary on 2026-08-02:

> P2-A is a rigid 2–10 component assembly MVP using one managed FCStd,
> explicit component identity/placement, per-component verification, and
> interference checks; defer native joints, cross-revision instances, BOM,
> TechDraw, and release packaging to later P2 slices.

P2-S04 has passed its real end-to-end product exit gate under the approved
minimum boundary above. The user has authorized publishing the P2 product.
Release preparation may proceed autonomously; the exact version, publication
channel, target commit, and recovery boundary must be verified before the
external publication effect.

Recovery boundary: P2-S01 through P2-S03 are complete at `5a1bb43`; the verified
P2-S04 publication candidate is the current intentional worktree slice. Commit
and branch publication are authorized. No external product release, deployment,
or installed-runtime mutation occurs until the exact release target is known.
Preserve the unrelated untracked paths.

After the P2 publication completes, start a separate WorkBuddy host-compatibility
slice. Reuse the same local stdio MCP, Skill, daemon, Task/Revision/Review, and
Release authority; add only the connection bundle or thin compatibility adapter
that WorkBuddy actually requires. Completion requires a real WorkBuddy session
covering tool discovery, one durable CAD task, ResourceLink/resources-read for
the P2 delivery package, review/approval, and restart recovery. The existing
`.workbuddy/` memory directory is not treated as connector configuration and
remains user-owned.

The read-only WorkBuddy 5.3.5 audit is recorded in
`docs/WORKBUDDY_COMPATIBILITY_RESEARCH.md`. It confirms host-level local stdio,
strict MCP startup, ResourceLink/list/read implementation, and current model
availability, while leaving binary PDF/ZIP round-tripping and the exact VibeCAD
restart workflow for the post-P2 live gate. The initial explicit-model matrix is
GLM-5.2 (default candidate), Kimi-K3 (quality ceiling), MiniMax-M3
(cost/performance), and DeepSeek-V4-Flash (fast/economy); `Auto` is excluded from
certification because opaque routing is not reproducible.
