# VibeCAD Active Plan — G1 Outcome Closeout

> Status: active / approved
>
> Updated: 2026-08-01
>
> Repository anchor: `codex/agent-stage3@b43b23c`
>
> This is the only mutable orchestration plan. Earlier campaign files are
> frozen historical records or future reference contracts; do not append
> command transcripts, retries, raw logs, or routine snapshots to them.

## 1. Outcome

Close G1 as one usable FreeCAD workflow rather than another infrastructure
stage. A user must be able to select a managed preview object, obtain its exact
VibeCAD selector, inspect the candidate, and Accept or Reject it. The same thin
Workbench must then complete one evidence-backed pilot inside a compatible
user-installed FreeCAD while the managed runtime remains the execution,
verification, and fallback authority.

G1 is complete when all of the following are true:

1. whole-object and feature selection produce exact, uniquely resolved `SelectorV1` values in the managed Workbench;
2. one admitted user-FreeCAD pilot connects through a bounded external bridge and completes the existing preview/review workflow;
3. the GUI gains no kernel or commit authority, and managed mode still works;
4. public docs and their contract tests describe the product that exists;
5. focused gates, one GUI observation per host, and one final settled suite prove the outcome without new validation infrastructure.

## 2. Current state

- Stage 3, P0-B, the multi-runtime foundation, and G1 C01-C03 are complete.
- The Workbench already provides lifecycle, Preview, and Accept/Reject against the authenticated local kernel.
- Managed installed-form Alpha is anchored at `83879b6`; repository-first and product-first docs at `656b27f` and `ebc29d9`.
- The dual-host product decision is recorded at `a4e5ab2`.
- C04 selector capture is accepted at `b43b23c`; the external bridge pilot is accepted at `91207b1`; user-host selector/review completion and documentation truth remain open.
- The topology review found an in-process `vibecad.daemon` import, while VibeCAD requires Python 3.12+ and the FreeCAD 1.1.3 pilot embeds Python 3.11.14. Installing the complete VibeCAD package there is not the pilot design.

## 3. Approved decisions and authority

The user approved this reset and execution order on 2026-08-01:

1. finish C04 in the verified managed Alpha before expanding dual-host work;
2. then prove one user-FreeCAD host through a small external VibeCAD bridge;
3. treat macOS `/Applications/FreeCAD.app`, currently 1.1.3, as the first evidence target—not a broad compatibility claim;
4. keep `vibecad --freecad` as the known-good managed path and fallback;
5. defer P1 breadth until the complete G1 user outcome is demonstrated.

This approval settles the sequence and bridge direction. The later instructions
to execute and continue autonomously authorize the approved G1 slices, their
bounded live pilot effects, and non-force publication. Reopen approval only for
a blocker or an unplanned product/transport/support-policy change.

Selected orchestration controls:

- **Continuity:** this file holds the compact cross-session state.
- **Approval:** the order and bridge direction are settled; reopen only if a branch condition changes product shape.
- **Delegation/review:** only for bounded independent work or an invariant that justifies a distinct reviewer.

No process controller, validation runner, scenario registry, evidence language,
or per-command ledger is selected.

## 4. Scope, exclusions, and invariants

In scope:

- C04 whole-object/feature selector capture in the Workbench;
- a minimal bridge, compatibility doctor, and reversible per-user install/uninstall for the single pilot;
- one managed-host and one user-host product observation;
- correction of stale G1 capability documentation and its contract tests.

Out of scope:

- Face/Edge selectors, topology persistence, and arbitrary model-generated FreeCAD Python;
- a second CAD, PartDesign/Mechanical3D breadth, and generic imported-model reconstruction;
- opportunistic `PATH` discovery, a compatibility matrix, marketplace publication, tags, or a general release;
- unrelated user files, `.workbuddy/`, and the two untracked CAD course documents.

Invariants:

- the Task Kernel remains the sole Task, Revision, HEAD, review, and commit authority;
- candidate mutation remains isolated and source models are never contaminated;
- selector construction uses tracked checkout identity and fails closed on malformed metadata, ambiguity, revision mismatch, or subelements;
- the least-authority plugin receives no daemon secrets in persistent configuration;
- installation never rewrites `FreeCAD.app`, preferences, macros, or unrelated addons, nor adopts an unknown existing tree;
- managed mode remains functional throughout the pilot.

## 5. Execution slices

### G1-00 — Plan reset

State: **complete** in the working tree; this file exists and the Stage 3, P0-B, and MRG1 rolling files are visibly frozen.

Gate: **GREEN** — the diff is consistent, the prior campaign records are frozen, and the exact next action is explicit.

### G1-01 — Managed C04 selector outcome

State: **accepted and pushed at `b43b23c`**.

Implement the already approved C04 contract:

- accept only a whole managed `DocumentObject` from a tracked Preview Document;
- map an object carrying `feature_id` to a feature-entity selector;
- derive project/revision only from the live checkout binding;
- construct through `parse_entity_identity()` and `EntityIdentity.to_selector()`, then uniquely `resolve_selector()` against the complete tracked document;
- reject Face/Edge subelements and every malformed, stale, mismatched, or ambiguous selection.

Outcome gate:

1. focused selection/controller tests pass;
2. one managed FreeCAD observation shows select → exact selector → existing Preview/Accept/Reject behavior;
3. the diff does not alter gateway transport or kernel authority.

If C04 requires gateway/transport redesign, move that requirement into G1-02; do not expand C04 to solve it.

Implemented result:

- the Workbench observes FreeCAD selection lifecycle and detaches the observer
  with its session;
- the Host accepts exactly one selected whole object from exactly one live
  tracked Preview binding and derives project/revision from that binding;
- identity construction and complete-document unique resolution use the
  existing selector authority; no Name/Label or Face/Edge fallback exists;
- the Dock shows a bounded canonical selector, copies that exact value, and
  clears or rejects it on selection, project/task, preview, or lifecycle drift;
- gateway transport, kernel authority, and review semantics are unchanged.

Evidence admitted for this slice:

1. settled selector/controller/Preview/Review/package focus: `332 passed`;
2. scoped Ruff, format, and `git diff --check`: GREEN;
3. the real managed FreeCAD GUI loaded the changed Workbench, reached active
   lifecycle with one Dock and the authenticated daemon, and exited with clean
   daemon retirement;
4. managed `FreeCADCmd` created a real `Part::Feature`; selector capture emitted
   canonical JSON and `resolve_selector()` returned that identical object.

The existing M00 harness then failed its legacy Refresh diagnostic because it
still observes `dock.request`, while hosted Refresh has used `_host_transport`
since the prior C03 anchor. This mismatch predates G1-01 and does not exercise
selector capture. It is recorded as a non-C04 diagnostic residual; no new
runner or harness repair was admitted. The real-GUI portion of the selector
outcome is therefore supported by the bounded combination of current-Workbench
activation, a real FreeCAD object round-trip, and product-level selection/review
integration tests rather than a new end-to-end GUI scenario.

### G1-02 — Single-host external-bridge pilot

State: **accepted at `91207b1`**.

Keep the Workbench self-contained in FreeCAD's Python/PySide environment. Run daemon-client/bootstrap logic in an external Python 3.12+ VibeCAD process over a bounded protocol.

The pilot must provide:

- explicit `--freecad-app` discovery and compatibility diagnostics;
- protocol/version handshake and actionable incompatible-host failure;
- per-user atomic install, ownership receipt, upgrade binding, and safe uninstall that refuses a mutated or foreign tree;
- no opportunistic `PATH` fallback and no broad support claim;
- an actionable managed-mode fallback.

Outcome gate:

1. focused bridge/discovery/install contract tests pass;
2. FreeCAD 1.1.3 connects to the authenticated daemon and the installed bridge
   carries the existing project/task/preview/review method set without kernel
   authority; the non-empty product outcome remains G1-03;
3. install/uninstall recovery is observed and one managed-mode regression remains green.

Fixed pilot contract:

- admit only an explicit absolute `.app` bundle whose exact observed metadata is the
  observed macOS FreeCAD `1.1.3`, embedded CPython `3.11`, and PySide6 `6.8.3`;
- keep the existing Workbench gateway, Preview, review, and checkout authority
  state machines inside FreeCAD, but proxy only the `LocalAgentClient` methods
  through one exact managed-Python `3.12+` child;
- use a versioned, bounded, length-prefixed canonical-JSON stdio protocol with
  an exact hello/ready handshake, monotonically increasing request ids, a closed
  method allowlist, fixed error codes, and one child per Workbench session;
- bind the child to the verified managed runtime executable and current VibeCAD
  package version; the addon receives no daemon receipt or secret and performs
  no direct Kernel connection;
- install only under the current user's FreeCAD `Mod/VibeCAD` directory from an
  exact payload allowlist, with a receipt binding host, bridge, hashes, and
  target; refuse an unknown existing tree, mutated owned payload, or mismatched
  uninstall request;
- keep `vibecad --freecad` unchanged as the managed fallback. The user-host CLI
  surface is exact: `--freecad-app <absolute.app>` plus one of `--doctor`,
  `--install-addon`, or `--uninstall-addon`; no `PATH` discovery is permitted.

Implemented result:

- the installed addon remains self-contained under FreeCAD's Python 3.11 and
  proxies only the closed `LocalAgentClient` surface through one managed Python
  3.12 child; persistent configuration contains executable identity, not daemon
  credentials;
- the child protocol has bounded canonical JSON frames, hello/ready nonce
  binding, monotonic request ids, fixed methods/errors, process retirement, and
  an environment allowlist;
- the doctor admits only the explicit observed 1.1.3/Python 3.11/PySide6 6.8.3
  bundle and records a stable fingerprint; no bundle execution or `PATH`
  discovery occurs during diagnosis;
- per-user installation is staged and atomic, binds host/payload/bridge hashes
  in an ownership receipt, supports same- and cross-package upgrades, and
  refuses foreign or mutated trees during upgrade or uninstall;
- the managed interpreter entry symlink, its canonical target, and target hash
  are bound separately so daemon bootstrap remains strict rather than admitting
  a resolved-but-unrecognized executable spelling.

Evidence admitted for this slice:

1. the settled bridge/install/launcher/Workbench/selector focus is `367 passed`;
2. scoped Ruff, format, and `git diff --check` are GREEN;
3. installed-form doctor admitted only `/Applications/FreeCAD.app` 1.1.3 and
   produced host fingerprint
   `ddfe5d97ceef9dfc93cdde01571207486416ad722e99fb588b29fe4059cb050c`;
4. the live installed addon completed bridge hello/ready, authenticated daemon
   ping, and project/task listing; the final bridge ping returned `ready`;
5. the real user FreeCAD GUI activated `VibeCADWorkbench`, reached one active
   connected Dock, and its process group was reaped after observation;
6. verified uninstall removed only the owned addon, and reinstall restored the
   exact final receipt
   `b8b92de7c1d79d516edf34c50c0250e3631c9e5ab435c921c7a89778cf42e5f2`.

The observed app has an invalid macOS code signature and some critical bundle
files are group-writable. The exact user-selected, user/root-owned,
non-world-writable bundle is therefore admitted only as this fingerprinted
local pilot, not as a general trust or compatibility claim. Missing
3DconnexionNavlib produced a non-blocking host warning during GUI startup.

The pilot data store contained zero projects and tasks. G1-02 therefore proves
the installed transport, lifecycle, list path, and reversible ownership, while
the non-empty user-host Preview/Accept-or-Reject outcome remains the explicit
G1-03 closeout gate rather than being inferred from this observation.

### G1-03 — Product closeout and truth alignment

- demonstrate C04 selector output plus Accept or Reject in the admitted user-FreeCAD pilot;
- align README, User Guide, Architecture, Product Capability Roadmap, and Acceptance Tests with Alpha, pilot, and deferred scope;
- remove assertions requiring the stale statement that G1 Workbench is undelivered;
- run one settled final relevant suite after product and docs stop changing.

Closeout gate: Section 1 is proven, residuals are recorded, and the next plan is a narrow P1 vertical slice.

## 6. Evidence budget

For each product slice, admit at most:

1. focused automated tests for changed behavior and invariants;
2. one real GUI observation for the affected host;
3. one independent review only when it changes an outcome or invariant
   decision.

Run the broader settled suite once at G1 closeout. A new runner, observer,
controller, harness, or evidence format is forbidden unless existing tests,
product logs, and direct observation cannot answer a named outcome or invariant;
any admitted aid must have a bounded scope and retirement condition. A missing
diagnostic explanation does not block an otherwise established outcome.

## 7. Recovery and exact next action

Recovery anchor: `codex/agent-stage3@91207b1`. Preserve the pre-existing
untracked `.workbuddy/` and both CAD course documents. On resume, inspect only
state and inputs that may have changed.

Exact next action in G1-03:

> Close the one known user-host gap: external FreeCAD cannot import the managed
> selector backend. Add the smallest bounded selector-authority path that keeps
> exact construction/unique resolution in managed code, then run one isolated
> non-empty user-host Preview plus Accept or Reject observation. After product
> behavior settles, align the five public capability documents and their
> existing contract tests; do not add another GUI harness or validation runner.

Material residuals entering G1-03:

- only one fingerprinted macOS FreeCAD 1.1.3 pilot target has local evidence;
- external FreeCAD selection currently fails closed because the managed
  selector backend is intentionally absent from the thin addon;
- no non-empty user-host Preview/Accept-or-Reject observation has yet been run;
- public docs still contain stale pre-G1 statements to correct at G1-03.

Additional residual after G1-01:

- the M00 real-GUI harness has a pre-existing hosted-Refresh observation
  mismatch and must not be mistaken for a selector failure or expanded into a
  second validation project.

Historical detail remains in:

- [`vibecad-agent-stage3.md`](vibecad-agent-stage3.md)
- [`vibecad-p0b-core.md`](vibecad-p0b-core.md)
- [`vibecad-multi-runtime-g1.md`](vibecad-multi-runtime-g1.md)
- [`vibecad-durable-v2.md`](vibecad-durable-v2.md) (future reference contract)
