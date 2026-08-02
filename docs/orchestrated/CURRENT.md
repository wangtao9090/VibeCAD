# VibeCAD Active Plan — P1 sequential user editing

> Status: **P1 complete; publication authorized**
>
> Updated: 2026-08-02
>
> Repository anchor: `codex/agent-stage3@b518f46`
>
> This is the only mutable orchestration plan. Earlier campaign files are
> frozen historical records; G1 closed at `b518f46` with the recorded
> `382 passed` final related suite.

## 1. Goal and success criteria

Deliver the smallest trustworthy workflow for the common product sequence:

```text
Agent edits isolated candidate
-> user reviews and Accepts or Rejects
-> Agent phase ends
-> user optionally makes small FreeCAD UI edits
-> explicit checkpoint verifies and publishes one new VibeCAD Revision
```

Success requires:

1. the Workbench clearly identifies Agent preview/review as non-editable product state and explains what happens if it is modified;
2. after the Agent phase, the user can open an editable working copy of the current HEAD;
3. dirty state is detected before another Agent task or publication decision;
4. explicit manual checkpoint creates a new user-origin candidate, reopens it, observes it, verifies it, and advances HEAD only through the existing lease/CAS authority;
5. no automatic user/Agent merge, rebase, conflict editor, or same-document concurrent editing is introduced;
6. product and architecture docs consistently keep Git optional and non-authoritative.

## 2. Approved decisions and authority

The user approved the following product boundary on 2026-08-01 and then asked
to execute it:

- collaboration is sequential ownership transfer, not concurrent editing;
- users are warned not to edit while VibeCAD is executing or presenting an Agent draft;
- edits made after the Agent phase may be explicitly checkpointed;
- unexpected simultaneous edits are detected and rejected, not automatically resolved;
- branch/worktree-style proposal isolation is deferred to future Agent Teams;
- research whether CAD files belong in Git and apply the result to this plan.

The request authorizes reversible implementation, tests, durable documentation,
and—on 2026-08-02—an intentional commit and push of this scope. It does not
authorize a PR, release, marketplace publication, dependency purchase, or
destructive cleanup.

Selected controls:

- **Continuity:** the work spans backend, protocol, Workbench, and tests, so this file is the compact recovery record.
- **Approval:** the product boundary above is settled; reopen only if implementation requires automatic merge, a second authority, or materially broader import support.
- **Gates:** use focused contract/integration tests and the existing final related suite; do not build a new validation framework.

No delegation, long-running process controller, production ledger, or
independent-review requirement is selected.

## 3. Scope, exclusions, and invariants

In scope:

- explicit Agent-owned preview state and user guidance;
- one editable HEAD checkout after the Agent phase;
- dirty/clean/stale/revoked state presentation;
- explicit discard/reload and checkpoint actions;
- a manual checkpoint kernel path with immutable base binding, project lease,
  private candidate, deterministic validation, fresh HEAD CAS, and rollback;
- an optional future Git export boundary for accepted revisions only.

Out of scope:

- automatic conflict resolution, semantic rebase, or background two-way sync;
- simultaneous edits to one FreeCAD document;
- multi-Agent branch merging, component ownership, or proposal selection UI;
- treating Git, GitHub, Git LFS, or a native CAD file as VibeCAD authority;
- unpack-and-merge of FCStd, generic native-CAD merge, or broad FCStd import;
- unrelated `.workbuddy/` and the two untracked CAD course documents.

Invariants:

- Task Kernel remains the sole Task, Revision, review, lease, HEAD, commit, and recovery authority;
- immutable revisions and Agent drafts are never edited in place;
- a checkout is non-authoritative and its ordinary FreeCAD Save never advances HEAD;
- an old draft verdict is never reused after user edits;
- dirty/stale/mismatched input fails closed and cannot overwrite a newer HEAD;
- source artifacts and unrelated user documents remain untouched;
- managed FreeCAD remains the default and the existing single external-host pilot claim does not expand.

## 4. Execution slices

### P1-S01 — Decision, research, and preview ownership UX

State: **complete**.

- record the sequential editing decision and CAD-in-Git research;
- change Workbench copy/state so Agent previews explicitly say not to edit;
- surface local modification as a recoverable discard/reload action, not a
  merge conflict;
- keep review disabled whenever the preview document or checkout is dirty.

Gate: focused Dock/Host/Preview tests, doc consistency, Ruff/format/diff check.

Result: the sequential ownership decision and Git/CAD storage policy are now
durable; the Dock identifies managed Agent previews as non-editable and keeps
the existing dirty/touched fail-closed review behavior. Focused controller gate:
`187 passed`; changed-file Ruff, format, and diff checks passed.

### P1-S02 — Editable HEAD working copy

State: **complete**.

- create a distinct editable checkout mode only from a live current HEAD;
- never reuse a review draft checkout for manual authoring;
- show clean, dirty, stale, and recovery-required states;
- require checkpoint or discard before starting another Agent-owned action.

Gate: managed checkout + protocol + Workbench integration tests prove that the
copy is non-authoritative, HEAD-neutral on Save, and stale after HEAD advances.

Result: the Dock now has a distinct editable role with **Open Editable HEAD**,
**Checkpoint Edit**, and **Discard Edit**. It is mutually exclusive with Agent
preview/review. Save remains local; a clean checkpoint is a no-op; successful
checkpoint closes the old document/checkout and refreshes the project. Both
auto-save-on-checkpoint and user-save-before-checkpoint paths are covered.

### P1-S03 — Manual checkpoint kernel path

State: **complete**.

- bind an idempotent checkpoint request to exact project, base HEAD, checkout
  identity, model digest, and size;
- copy only the exact dirty managed file into a private candidate;
- reopen, normalize/checkpoint, export STEP, seal, observe, and verify the
  P1 single-part envelope;
- publish only after fresh lease and complete base HEAD CAS;
- produce a normal durable task/revision outcome with user-origin provenance;
- fail closed on clean, closed, stale, revoked, replaced, oversized, malformed,
  unsupported, or concurrently changed input.

Gate: focused Task Kernel tests plus a real managed FreeCAD checkpoint of one
small post-Accept parameter edit. Failure and stale-base cases leave HEAD and
the prior accepted revision unchanged.

Result: `system.checkpoint_checkout` reserves an immutable request bound
to exact checkout, base HEAD, digest, and size; the checkout store stages only
that exact file into a private candidate; the normal export, evidence,
verification, Revision, and HEAD CAS path publishes it. Source-stage failure,
idempotent replay, key rebinding, stale HEAD, and rejection all preserve the
prior authoritative state.

Real gate: the existing cross-process managed FreeCAD harness completed a
post-Accept Box edit from length 10 to 14, detected the checkout as dirty,
published verified FCStd/STEP, advanced HEAD generation 1 → 2, reloaded volume
`8400 mm³`, and preserved the accepted base Revision hash: `1 passed, 10
deselected`.

### P1-S04 — Product integration and closeout

State: **complete**.

- connect the Workbench editable action to the public local client without
  exposing local paths or daemon secrets;
- update user guide and capability truth;
- run the final affected suite and one bounded GUI observation;
- close this plan only when the full sequential outcome works.

Gate: user-visible Agent -> review -> Accept -> manual edit -> checkpoint ->
new HEAD outcome, with no automatic merge path and no regression to G1 review.

Computer-use result: an isolated normal managed FreeCAD launch connected to the
real local daemon, opened the accepted editable HEAD, selected `Box`, changed
Length 10 → 14, clicked **Checkpoint Edit**, closed the old checkout, and
refreshed the project while the Workbench remained Connected. HEAD advanced
generation 1 → 2 and a fresh managed checkout reopened as `14 × 20 × 30`, volume
`8400 mm³`. The Workbench's 10-second ping kept the worker-owned connection alive
past the daemon's 30-second idle timeout without consuming protocol request IDs.

The GUI run also exposed a real FreeCAD state nuance: recompute clears
`Document.isTouched()` before Save. The editable flow now observes document and
object mutations from the moment the editable binding opens, so a recomputed but
unsaved change is still saved at checkpoint. A clean checkpoint still skips Save,
avoiding a false revision caused solely by FCStd reopen/reserialization bytes.

## 5. Current state and next action

Completed facts:

- G1 is complete at `b518f46` and local/upstream were equal at this campaign anchor;
- existing managed checkout already computes content dirty state and source liveness;
- existing PreviewCoordinator already rejects touched FreeCAD documents and dirty checkout descriptors for review;
- existing draft Accept re-verifies and applies HEAD CAS;
- Git/LFS research concludes that Git is an optional export/mirror, not the live CAD database or merge authority.
- the exact checkpoint method is wired through AgentApplication, protocol v2,
  daemon facade, local client, bounded external bridge, gateway, Host, and Dock
  without exposing a checkout path on the wire;
- affected Workbench/bridge regression is `198 passed`; the application,
  protocol, checkout, revision, checkpoint, Task Kernel, and local daemon
  regression is `860 passed, 1 deselected` (two expected platform warnings).
- real FreeCAD exposed two save byproducts that are now handled at their narrow
  authority boundaries: Workbench restores the exact granted document to
  private mode after owner/no-follow/single-link checks, and checkout cleanup
  accepts only a bounded timestamped `.FCBak` name set while continuing to
  reject unknown extras;
- the Workbench keeps its authenticated daemon connection alive on the existing
  worker thread while idle, stops the timer during cleanup, and fails closed if
  the ping itself fails; focused Workbench controller regression is `195 passed`;
- the settled repository default suite is `5568 passed, 110 deselected` with
  19 expected platform/security-test warnings; full Ruff, changed-file format,
  and `git diff --check` pass. Full-repository format still reports 52
  pre-existing unrelated files and was intentionally not rewritten.
- the normal interactive GUI gate passed with a real parameter mutation,
  checkpoint, authoritative generation advance, and FreeCAD-kernel geometry
  verification; P1 is therefore closed.

Next action:

1. stage only the P1 implementation, tests, and documentation while preserving
   `.workbuddy/` and the two unrelated CAD course documents;
2. commit the completed sequential FreeCAD editing workflow and push the current
   `codex/agent-stage3` branch;
3. do not open a PR or publish a release without separate authorization.

Recovery boundary: the P1 implementation is complete and authorized for branch
publication only. Preserve the three pre-existing untracked paths.
