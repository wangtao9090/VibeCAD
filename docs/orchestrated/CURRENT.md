# VibeCAD Active Plan — P2 mechanical delivery

> Status: **P2-A approved; P2-S01 complete; P2-S02 active**
>
> Updated: 2026-08-02
>
> Repository anchor: `codex/agent-stage3@7f3d506`
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

The current public semantic operation registry still contains six operations:
`create_box`, `create_cylinder`, `modify_parameter`, `move_part`, `rotate_part`,
and `inspect_model`. It remains a single-part product surface.

The repository also contains a non-public legacy Round-8 assembly implementation
with real FreeCAD evidence for:

- `App::Part` multi-part containment;
- per-part feature chains and rigid placement;
- planar face alignment;
- pairwise interference detection;
- assembly-aware description, rendering, and STEP export.

Those old Session/server semantics are deliberately excluded from the current
Task Kernel public surface. They are reusable implementation evidence, not an
alternative write authority and not a product capability until migrated through
ModelProgram, candidate verification, Revision, review, and HEAD CAS.

## 2. Recommended P2 product boundary

Start with **P2-A: rigid multi-part delivery MVP**. This is the smallest slice
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
- flat/structured BOM, TechDraw, release approval, and release package in the
  first implementation slice;
- a second CAD backend, durable schema migration, release/tag, or PR.

The last four mechanical-delivery capabilities remain part of the P2 campaign,
but enter only after the assembly authority and verifier foundation is proven.

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

State: **active**.

- add minimal component creation and explicit-target primitive operations;
- add deterministic absolute component placement;
- calculate pairwise interference from reloaded global shapes;
- verify per-component preservation, total assembly geometry, and requested
  interference policy;
- deliver one accepted two-component FCStd/STEP through the existing review path.

Gate: focused Task Kernel integration plus one real managed FreeCAD outcome.
No new test controller or validation framework is admitted.

### P2-S03 — Flat BOM

State: **pending after P2-S02**.

- add bounded part number, description, material/density, and quantity metadata;
- derive a flat BOM from sealed component facts;
- bind BOM to the exact accepted Revision and include machine-readable output;
- do not claim where-used, external part master, configurations, or PLM.

### P2-S04 — TechDraw and release package

State: **pending after P2-S03**.

- produce a bounded drawing template with assembly views, balloons, and revision
  identity;
- package verified FCStd, STEP, flat BOM, drawing export, manifest, and validation
  report;
- distinguish an immutable VibeCAD Revision from a later release approval state.

Native joints/DOF solving can be evaluated only after this rigid-delivery path
is stable; it is not silently included in P2-S04.

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
- **Approval:** confirm the P2-A product boundary before changing public component
  semantics or broadening the accepted model envelope.
- **Gates:** G1 focused contracts for S01; G2 Task Kernel integration for S02;
  one G3 real managed FreeCAD assembly outcome before calling P2-A complete.

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

The next action is P2-S02: add the smallest public vertical slice for explicit
component creation, component-targeted primitive creation, absolute placement,
and pairwise interference, all through the existing candidate/review/Revision
authority. The public-surface fingerprint and runtime receipt may change only
with the deliberate public operation addition.

Recovery boundary: P2 executable work is now authorized within P2-S02, as are
intentional commits and branch pushes after declared gates pass. No durable
schema migration, PR, release, or deployment is authorized. The P2 campaign
start anchor remains `7f3d506`; preserve the unrelated untracked paths.
