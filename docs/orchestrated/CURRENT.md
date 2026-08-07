# VibeCAD Active Plan — Visual CAD

> Status: **v0.8.0 published; S41 and S42 complete**
>
> Updated: 2026-08-07
>
> Repository anchor: `main@5950da2`; published tag `v0.8.0@6ee230f`
>
> Active plan: [`vibecad-visual-cad.md`](vibecad-visual-cad.md)
>
> The completed P2 and WorkBuddy closeout remains below as historical context.
> Earlier campaign files remain historical records.

## 0. Active Visual CAD gate

The approved design direction separates two product tracks:

- Mechanical Parametric: dimensioned single/multi-view images produce true
  Sketcher constraints and bounded PartDesign features;
- Freeform: industrial surfaces use section/guide curves and Loft/Sweep/NURBS,
  while sculpture-class outputs remain Mesh/SubD derived artifacts unless a
  later durable artifact decision admits them.

The DCC/mesh tooling research is now captured in
[`VISUAL_CAD_TOOLING_RESEARCH.md`](../VISUAL_CAD_TOOLING_RESEARCH.md): Blender is
the first external sculpture-host candidate, Open3D is the preferred permissive
algorithm library, and GPL PyMeshLab remains optional pending license review.
This research does not expand `VCAD-A02`.

On 2026-08-07 the user approved S42's bounded semantic edge-treatment slice.
The public MCP, Task, Revision, HEAD, durable schema, runtime epoch, and
SelectorV1 contracts stay unchanged. ParametricDesignIR v1 gains one optional
Fillet/Chamfer tail after its PartDesign Body: one to sixteen semantic targets
per treatment, at most eight combined PartDesign/treatment nodes, constant and
per-edge Fillet, one linear start/end Fillet law on an oriented nonclosed edge,
and symmetric per-edge Chamfer. Selection is reconstructed from source feature,
source sketch geometry, and section/sweep role; no transient EdgeN is durable or
public. Ambiguity, missing topology, variable-edge direction reversal, or invalid
kernel geometry fails the whole transaction closed. Arbitrary imported STEP
edges, tangent-chain propagation, variable Chamfer, multi-point radius laws,
complex blends, semantic merge, and manual-topology repair remain outside S42.

PR #18 merged this slice as `5950da2a852b3f3b1fa530b6bf06de9f7504d32b` after
`lint-unit` and `runtime-integration` passed in Actions run `31173378976`.
The isolated test runtime, caches, and probes were removed; the user FreeCAD app
and unrelated untracked files remain untouched. S42 is complete.

On 2026-08-04 the user approved `VCAD-A04`. The public V1 envelope is now
frozen to a single dimension-complete mechanical extruded or revolved part,
with editable Sketcher plus bounded PartDesign output and deterministic
dimension, BRep, and single-solid verification. Unscaled, conflicting,
occluded, or hidden structure must clarify or stop as `SAFE_FAILURE`; it must
not be promoted to confirmed geometry. This approval activates S35 for two to
sixteen clean complementary views of the same object, state, and scale, but it
does not activate ordinary-photo S40, Freeform, sculpture, or publication.

S35 now closes that exact outcome: a three-view L bracket reached a verified
review draft with three fully constrained sketches, one Pad, and three proven
through-hole axes; two negative fixture sets stopped on missing extrusion depth
and conflicting width. WorkBuddy extracted the correct visual facts but its
first handwritten IR required bounded strict-contract corrections, so the
evidence does not claim zero-repair authoring.

Final visual QA found that the original positive fixture itself had a mismatched
printed scale and ambiguous hole-coordinate baselines. The settled three-view
fixture now uses one 4:1 scale and explicit lower-left-origin coordinates. A
fresh, answer-free, Read-only GLM-5V-Turbo replay returned `PASS`, all exact
dimensions and hole centers, no conflicts, and no blocking unknowns. It was a
visual-evidence replay only; it did not create or mutate a CAD Task.

The published v0.7.0 release passes 5,932 repository non-slow tests,
the fresh-unpacked MCPB real-FreeCAD/resource gate, Ruff, changed-file format,
compileall, version/diff integrity, MCPB manifest validation, Twine, fresh
Python 3.12 wheel/sdist imports, and source/sdist/MCPB/standalone-Skill byte
parity. Real Codex, Claude, and WorkBuddy fresh-process smokes each recovered a
generation-9 review task, accepted it at generation 11, and read both FCStd and
STEP resources. The Codex run exposed a bounded MCP compatibility gap:
`tools/list` may carry `_meta.progressToken`; the transport now accepts and
strips valid request/notification metadata while remaining strict for all
other unknown fields. A GitHub macOS-only atime failure was also corrected
without weakening dev/inode/mode/owner/size/mtime/ctime mutation checks; the
rebuilt final MCPB passed the complete host-neutral packed gate. PR #11 merged
as `6bcd934`; tag `v0.7.0` resolves to the same commit. GitHub Actions run
[`30982172060`](https://github.com/wangtao9090/VibeCAD/actions/runs/30982172060)
published the [v0.7.0 GitHub Release](https://github.com/wangtao9090/VibeCAD/releases/tag/v0.7.0)
and [PyPI 0.7.0](https://pypi.org/project/vibecad/0.7.0/). Downloaded Release
assets matched GitHub's SHA-256 metadata and unpacked cleanly; a fresh Python
3.12 environment installed `vibecad==0.7.0` from the public PyPI index. The
wheel and sdist hashes match the final local candidates exactly. All A06
temporary host homes, MCP/Skill installs, WorkBuddy sessions/traces, four test
daemons, package candidates, and both VibeCAD-created FreeCAD runtimes/caches
were then removed while durable data and repository/user-owned files remained.
WorkBuddy alone retains its additional strict-error/bounded-submit compatibility
gate; it does not substitute for either other host.

On 2026-08-05 the user selected ordinary-photo mechanical reconstruction as
the next product track and approved S40. The first slice keeps the existing
authority unchanged: the multimodal host screens capture quality, absolute
scale, perspective, occlusion, and geometry completeness; VibeCAD still receives
only one confirmed bounded ParametricDesignIR through the ordinary
`REQUIRE_REVIEW` Task Kernel. It adds no MCP tool, durable status, CAD operation,
or second write authority. The new host-local outcomes are `PHOTO_READY`,
`NEEDS_CAPTURE`, and `OUT_OF_ENVELOPE`; only `PHOTO_READY` may reach
`create_task`. Real-photo support remains a branch-candidate claim until three
license-clear, metadata-free ordinary-photo samples with independently confirmed
dimensions and two negative cases pass the declared host and FreeCAD gates.

S40.1 now closes the reusable guidance layer. The canonical Skill links one
portable Guided Photo v1 reference covering the supported envelope, capture
roles for extruded/revolved parts, coplanar scale rules, direct measurements,
capture/scale/geometry-completeness gates, candidate-branch confirmation, and
fresh replanning. The optional VibeCAD-managed Provider prompt uses the same
recapture/measurement and coplanar-scale rules without gaining CAD authority.
The affected deterministic gate is 254 passed, plus Skill validation, Ruff,
and diff integrity. S40.2 cannot establish a real-photo product outcome from
synthetic renders alone. Public web photos are admissible only when their
license and provenance are recorded and their dimensions come from independent
confirmed facts or evaluator-only references rather than visual metrology.

The first private S40.2 sample has eleven complementary ordinary photos and one
user-supplied STEP reference. The reference remains evaluator-only: the host
photo-analysis prompt and generated candidate never receive the STEP geometry,
topology, or file content. The STEP itself, detailed measurements, private
photos, and generated private artifacts remain outside repository fixtures.
The approved minimum correction is now implemented: one axis-aligned `slot`
stays one IR geometry but compiles to two native lines, two semicircular arcs,
and fourteen deterministic editable Sketcher constraints. Oblique slots and IR
constraints that target an atomic slot fail before CAD mutation. The existing
3,500-node IR and ModelProgram budgets are unchanged; the private six-feature
candidate is 2,603 nodes instead of the hand-expanded prototype's roughly
4,545 nodes. Real FreeCAD proved all six sketches fully constrained with
`DoF=0`, four native edges and fourteen constraints per slot, and one valid
solid. A FreeCAD-style native constraint edit changed the slot width from 6 mm
to 8 mm; a subsequent public depth-parameter edit from 8 mm to 10 mm preserved
that manual change and solver closure. The ordinary Task Kernel reached
generation 9 `awaiting_user_review`;
all four deterministic verifiers passed and HEAD remained unchanged. Only
after candidate completion, the isolated evaluator compared it with the hidden
normalized reference and measured volume IoU `0.937880`. Exact 3D fillets and
chamfers remain outside v1. This private pilot proves the implementation path
but does not replace the license-clear, metadata-free fixed three-positive and
two-negative S40.2 gate.

S40.2 and S40.3 now close that fixed gate. The committed public fixture set has
three metadata-free ordinary-photo positives: an annular washer, a rounded-square
120 mm fan spacer, and a calibration block with one blind pocket. Its two
negatives reuse the washer without thickness (`NEEDS_CAPTURE`) and use a CC0
multi-object clutter frame (`OUT_OF_ENVELOPE`); neither negative created a Task.
Source pages, authors, licenses, original/normalized hashes, host-visible facts,
expected routing, and evaluator-only truth are separated in the fixture tree.
The public mesh references are recorded by digest but are not committed or shown
to a host. The user's private blade photos, STEP, and candidate remain outside
the repository.

Fresh real-host runs each proved a distinct positive through the same stdio MCP
and managed FreeCAD authority: Claude produced the 30 × 20 × 10 mm calibration
block, WorkBuddy/GLM-5V-Turbo produced the 20 × 20 × 2 mm washer, and Codex
produced the 120 × 120 × 5 mm fan spacer. All reached generation 9
`awaiting_user_review`, left HEAD unchanged, passed bounding-box, volume,
valid-BRep, and one-solid verification, and emitted FCStd plus STEP resources.
Real FreeCAD also proved every sketch fully constrained and a depth edit changed
the solid while preserving validity. The host runs exposed two bounded product
defects now covered by regression tests: an idle authenticated daemon connection
is reopened once and the exact request replayed once, and the complex but bounded
fan program now fits a 128-parameter/8,192-node internal envelope. The canonical
Skill's rounded-rectangle recipe now constrains arc centers and tangential
endpoints without redundant radial constraints.
The branch gate is 5,942 non-slow tests passed / 126 deselected, 10 real-FreeCAD
slow outcomes passed, plus Ruff, changed-file format, compile, diff, and Skill
validation.
All three temporary daemons were authenticated-retired; the temporary
Codex/Claude/WorkBuddy homes, copied Skills/MCP configuration, source downloads,
diagnostics, and test-created FreeCAD runtime were then removed. The user's
installed `/Applications/FreeCAD.app`, private source files, and unrelated
untracked repository files were preserved.

On 2026-08-06 the user approved the next plan: publish the completed S40 boundary as v0.8.0,
then begin S41 derived-expression parameter linkage. `VCAD-A08` authorizes the release branch,
PR/merge, `v0.8.0` tag, GitHub Release and PyPI publication. The release candidate must be built
from tracked committed content in an isolated checkout because unrelated untracked duplicate files
appeared in the working tree; those files remain user-owned and untouched. After the candidate passes
the exact package gates, Codex, Claude, and WorkBuddy must each consume the same candidate identity.
PyPI version identity is immutable, so a failure after publication starts is recovered by observing
or forward-fixing the workflow, never by overwriting a published artifact.

The tracked-content candidate is now frozen at `codex/v0.8.0-release@67a662b`. Its MCPB SHA-256 is
`8c3d804a8e7bc153d45040a885cd2de8a7183e0f4109533af3683594319ac7b6`; the wheel and sdist are
`badc8f57ff7d69efd16e905424f05b7ff186cb2b99f01d3d198e08ac4f435826` and
`3436216c45055c38e52cc01cf71956caae37340bcaec6de709d8552ed04ad1dc`. The isolated full gate is
`5,942 passed, 126 deselected`, and the exact unpacked candidate passed the package, managed-runtime,
real-FreeCAD, release-workflow, and Guided Photo gates. Fresh Codex, Claude, and WorkBuddy processes
each created a 60 x 40 x 10 mm review task, observed all four deterministic checks pass at generation
9 with unchanged HEAD, restarted, accepted at generation 11, exported FCStd/STEP, and read both MCP
binary resources. WorkBuddy additionally proved deferred `ToolSearch` schema discovery and native
`ReadMcpResource` blob persistence; local byte counts and SHA-256 recomputation matched its manifest.
The local MCPB archive digest above is build-instance-specific because the upstream MCPB packer records
pack time. The public archive is `eb044e0c14455e482d3cd99f3746ceffe1b0cc7657accb2761b03bf713820313`;
both archives have the same 1,006,247-byte size and the same 152 unpacked files byte-for-byte, with a
normalized relative-file manifest digest of
`b285cded13f7a11d8bc5d80ae4c717ff0695d79c5baaca1e12b903677c1dffdd`. The release workflow also
ran its packed MCPB stdio/resource gate on the public archive itself.

PR #14 merged as `6ee230f13dc63120cbc59f5bd1e5ff72a6ca6542`, and tag `v0.8.0` points to
that exact merge commit. GitHub Actions run `31145385088` passed the version, full quality, package,
two managed-Agent, packed MCPB resource, PyPI, and GitHub Release jobs. The published wheel/sdist
hashes exactly match the local candidates; the standalone Skill is
`155be4fb8d73b241d60bc90d3b7cfaffb88d0b1ec49787e8bf6853924d6bceda`. A fresh isolated Python
3.12 environment installed `vibecad==0.8.0` from the public PyPI index and exposed the expected 38
tools from `site-packages`. Four test daemons/workers then exited, and approximately 4.7 GB of
VibeCAD-created runtime, candidate/public-install roots, host-local Skill/MCP configuration, Resource
blobs, and exact Codex/Claude/WorkBuddy test records were removed. The user-owned FreeCAD app and all
untracked repository files remain. `VCAD-A08` is complete; the next implementation action is S41.

S41 now implements the bounded derived-expression linkage approved after v0.8.0. A parameter may
optionally carry a structured affine expression with one to eight same-unit source terms and a finite
constant. Derived parameters are private, acyclic, and must declare an initial value that matches the
expression; legacy independent parameters retain their exact serialized shape. The compiler installs
these relationships as native FreeCAD `ExpressionEngine` bindings on the existing parameter carrier,
authenticates strict expression metadata on reopen, follows transitive consumers, and rejects invalid
live derived values before starting CAD mutation. It does not add raw expression strings, an MCP tool,
a durable schema, a Task state, a CAD authority, or a version bump.

The rounded-square fan fixture now derives straight lengths and arc/edge coordinates from public
width, height, radius, and private origins. Real managed FreeCAD proved sequential width `120 -> 130`
and radius `8 -> 10` edits: derived straight width changed `104 -> 114 -> 110`, the solid remained a
valid single BRep with `130 x 120 x 5 mm` bounds, and the affected consumer stayed the outer sketch.
An invalid width of `10 mm` failed before mutation and preserved the prior volume. The isolated tracked
suite is `5,947 passed, 127 deselected`; the focused contract/fixture/Skill gate is `75 passed, 11
deselected`, with Ruff, format, compile, diff, and Skill validation also green. PR #16 merged as
`3199c61a727a7ed20827ee6372187d2434bc773d`; GitHub CI `lint-unit` and `runtime-integration` passed in
2m21s and 1m55s. User-owned duplicate files and
`/Applications/FreeCAD.app` remain untouched; the temporary managed runtime was removed.

The complete architecture, slices, gates, privacy boundary, validation budget,
and recovery point are in
[`vibecad-visual-cad.md`](vibecad-visual-cad.md). VCAD-A07 completed S40 and VCAD-A08
published it as v0.8.0; S41 starts from the post-release closeout anchor `main@9d39547`. S10.1 froze the
minimal ParametricDesignIR v1; S10.2 delivered native Sketcher objects and
solver/DoF facts. S10.3 now compiles closed profiles into a strict single-body
Pad/Pocket/Revolution/Hole chain, preserves feature parameter expressions and
IR mappings across FCStd reload, and rejects invalid, multi-solid, stale, or
no-op feature outcomes. S35 keeps Pocket at exactly one live wire per feature
and admits 1–16 same-plane Hole circles only when diameter, extent/depth, and
direction are shared; every declared axis receives its own material-removal
proof. S10.4 carries this complete IR through one hidden,
atomic ModelProgram/Task/Worker operation. The compiler adopts stable
Body/feature EntityIdentity inside the same FreeCAD transaction, stabilizes
parametric state before observation/checkpoint/export, and produces an ordinary
review draft without advancing HEAD. S10.5 adds one hidden
`modify_parametric_parameter` operation: a revision-bound Body selector, the
immutable source IR, parameter ID, and finite value drive one native carrier
edit. The compiler revalidates the effective live IR and reads every affected
Sketcher/PartDesign consumer back before an in-transaction executor verifier
admits the result. R1 and R2 retain the same Body/feature identities while the
old Revision remains byte-immutable. It adds no direct MCP tool or second write
authority; the MCP tool count remains 38.
Visual persistence is approved under A02 and ImageSet sealing is implemented.
A03 authorized a provider-neutral cloud-VLM adapter, and v0.7.0 implements
1–16 source images, sealed read-only cloud access, adaptive metadata-free
derivatives/crops, and one concrete OpenAI Responses transport. The user has since
reaffirmed the older Agent-first product boundary: Codex, Claude, WorkBuddy, or another
calling host owns image understanding, model selection, subscription, and credentials;
VibeCAD owns the CAD Task Kernel. The direct Provider path is therefore optional and
non-default, not the primary image-to-CAD path or a release blocker. The A04 public
mechanical envelope is published; S40 is complete under VCAD-A07 and is entering v0.8.0 under
VCAD-A08, while
Freeform remains behind A05.

S20.1 seals descriptor-bound local JPEG/PNG ImageSets under the additive
`visual_inputs/` root with provenance, byte/pixel budgets, normalization, and
atomic no-replace publication. S20.2 adds provider-neutral visual claims,
observations, clarification answers, evidence-complete reconstruction proposals,
and deterministic lifecycle actions. It does not create a CAD candidate or give
the Provider any Task, Accept, commit, or HEAD authority. S20.3 provides the
identity-pinned ReconstructionDraft store, intent-before-start Visual Domain
Service, separate result retrieval, deterministic fake-provider composition,
and restart-safe reconcile-only recovery. S20.4 closes answer authority binding,
explicit retry from FAILED, reject, and adoption through an application-owned
trusted port into an ordinary `REQUIRE_REVIEW` CAD Task. Adoption restart recovery
reconciles the durable intent and never replays an unknown create. Delete advances
through three durable phases around source-byte removal, then replaces the
transient exact marker with a permanent ID-only retired tombstone. That tombstone
contains no manifest/source hash or path, permanently prevents reuse of the same
ImageSet ID, and shares the 1,024-identity lifetime budget with ReconstructionDraft
tombstones. The focused S20.3/S20.4 gate is `297 passed, 1 deselected`. S20.5 now exposes exactly
seven strict, host-neutral reconstruction actions through the existing Agent application, daemon,
and MCP authority; v0.7.0 has 38 public tools. A separate non-MCP local host adapter
seals one to sixteen JPEG/PNG inputs through one authenticated staging-directory descriptor without
placing paths, filenames, base64, or image bytes on the JSON wire. The integrated S20.5 gate is
`488 passed, 2 deselected`; the isolated real worker and real four-image reconnect/restart replay
gates are `1 passed` each; the final repository suite is `5,875 passed, 119 deselected`; static
checks pass and independent review reports no P0/P1 findings. The fixed discovery frame is 30,415
bytes. S20.5 landed as deterministic-fake/interface-ready; S30.1 later added an opt-in cloud
Provider while the application default remains fake. Direct WorkBuddy attachment ingress into the
sealed store remains unverified, but the primary host-owned image path does not require it.

On 2026-08-04 the user approved `VCAD-A03`: cloud image transfer is allowed without a per-task
confirmation; Provider retention is allowed under the selected personal or enterprise account
policy; local deletion is not expected to retract an already transmitted Provider copy; and no
user-facing dollar, call-count, or wall-clock budget is required for the pilot. Engineering safety
limits remain mandatory but are not a product spending policy: one durable intent owns at most one
in-flight Provider effect, transport has a finite timeout and bounded payload/result, an automatic
retry is allowed only when non-acceptance is proved, and an unknown outcome enters recovery rather
than a recursive retry loop. Originals remain sealed locally; Provider adapters may create
model-specific resized images and detail crops. Sixteen is an input ceiling, not a claim that
duplicate, blurry, or contradictory views improve reconstruction. The S30.1 candidate now accepts
1–16 images, but application default composition remains the deterministic fake Provider. The opt-in
OpenAI transport is not a public product claim and has not made a live API call. That missing
direct-transport evidence is now optional: it does not block a host-owned vision pilot because the
calling Agent already owns its multimodal inference channel.

The S30.1 implementation keeps originals sealed locally and produces bounded PNG derivatives. The
quality-first OpenAI pilot profile uses 2,048 px overview long edges, permits explicit detail crops,
and caps a source set at 16 views; `original` detail preserves the controlled derivative rather than
blindly transmitting every original-resolution image. The derivative API can preserve a
caller-selected dimension, hole, thread, or boundary crop, but automatic crop selection is not yet
wired into the Provider run; the current
OpenAI path sends overview derivatives only. One durable invocation causes at most one
transport effect. Transport exceptions become `UNKNOWN`, reconciliation never replays the call, and
definitive HTTP/contract failures become terminal results. Successful cloud results carry the
request, derivative-batch, response-ID, structured-output digests, actual returned model, token
counts, data-policy profile, and finite timeout in provenance; credentials and raw provider IDs are
not persisted. Offline evidence is 203 visual tests passed with one real-daemon test deselected, that
real-daemon test passed separately, affected host regression is 369 passed/1 deselected, and the full
repository gate is 5,897 passed/119 deselected. S30.2 now validates the actual product route: the
calling multimodal Agent analyzes a self-authored CAD reference, classifies confirmed/inferred/unknown
facts, asks only blocking questions, and then uses the existing `create_task` →
`submit_model_program` → review flow. No VibeCAD API key or second model upload is part of that gate.
The positive host-derived pilot produced an 80 × 50 × 8 mm plate with one centered Ø10 through
hole as two fully constrained sketches and Pad/Hole features. A real managed-FreeCAD Task reached
`awaiting_user_review` while HEAD stayed unchanged; volume `31371.681469282037 mm^3`, bounding box,
valid-shape, and single-solid verdicts all passed, and 16,387-byte FCStd plus 8,311-byte STEP
artifacts were materialized. The incomplete assembly image correctly stopped before Task creation.
S30.3 adds a portable `ParametricDesignIR v1` authoring reference inside the canonical skill because
`get_capabilities` names the value shape but intentionally does not expand its nested wire contract.
No new MCP tool or state machine was added. The fixed fixture set now has two positive and two
SAFE_FAILURE outcomes. The stepped-shaft image produced a fully constrained 23-constraint half
profile and 360-degree Revolution; a real Task reached `awaiting_user_review` with HEAD unchanged,
and volume `28792.696670150453 mm^3`, `70 × 30 × 30 mm` bounding box, valid-shape, and single-solid
verdicts all passed. The unscaled bracket and 80/75-mm conflicting plate correctly stopped before
Task creation.

S30.4 closes the real WorkBuddy host path. Transparent stdio capture proved
that 9–11 KiB ModelProgram arguments reached VibeCAD intact; the failure was not
an MCP payload limit. WorkBuddy 5.3.5 obscured/retried strict domain failures and
one later application-backed submission surfaced only generic `-32603`, so the
branch adds one bounded `vibecad --workbuddy-submit` file adapter. It accepts
only one owned, non-symlink project-local JSON request, reuses the canonical
ModelProgram/ParametricDesignIR contracts, and calls the existing same-user
daemon; it adds no MCP tool, Task state, CAD operation, or second authority.

With permissions restricted to the fixture/Skill reads, one request-file write,
one exact adapter command, and six named MCP operations, GLM-5V-Turbo created
Task `task_4a5520dd7e3b9289eacf873565f71dd4`. It reached generation 9 and
`awaiting_user_review` with no error, while HEAD remained at base Revision
`revision_7d20d63e0b628c77c2b2aad3091cdcfd`. Bbox `80 × 50 × 8 mm`, volume
`31371.681469282037 mm^3`, valid shape, and one solid all passed; the unaccepted
draft contains 16,337-byte FCStd and 8,311-byte STEP artifacts. A fresh
read-only WorkBuddy process with only `get_task` permission recovered the exact
generation, candidate, draft, next action, and null error. This completes
the frozen single-image alpha envelope, not general photo reconstruction.

S10.4 closeout evidence is bounded to the product seam: a 3,405-node IR durable
round-trip; four real managed-FreeCAD outcomes covering atomic rollback, the
exact 26-object maximum, Worker checkpoint/STEP/reload, and Task review without
HEAD advance; `5,672 passed, 118 deselected` in the final non-slow suite plus
static/package/isolated-wheel gates; and two clean independent reviews. The
capability fingerprint changed with the new hidden
operation, so stale runtime receipts fail closed through the existing surface
digest while private epoch 4 and public version 0.6.1 remain unchanged.

S10.5 closeout keeps the additional evidence at the product seam: the existing
3,405-node IR produces a 3,526-node durable modify TaskRun without widening the
4,096-node budget; five real managed-FreeCAD outcomes cover adoption rollback,
edit-verifier rollback through a Sketcher-bound parameter, the exact 26-object
maximum, Worker reload, and the full R1 create/Accept → R2 modify/Accept flow.
That final flow reopens both FCStd revisions, imports the new STEP with
`Part.read`, preserves identities and the complete R1 tree, and proves the
8→12 mm native Pad change. No new value shape or public MCP schema was added,
so the public surface digest remains unchanged. The final gate was 5,673 passed
/ 119 deselected, plus Ruff, compileall, diff/package/isolated-wheel
checks and clean independent review; the pushed S10.5 anchor is `7dfddce`.

## 1. Completed P2 product truth (historical)

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

The public semantic operation registry now contains eleven operations. The six
existing direct operations remain direct-exposed. Five ModelProgram-only
operations, `create_component`, `place_component`, `set_component_bom`, and
`create_parametric_design` plus `modify_parametric_parameter`, establish the
bounded assembly/flat-BOM path and native parametric creation/editing;
`create_box` and
`create_cylinder` accept an optional explicit component target. The published `v0.6.1` MCP surface
contains 31 tools: the prior 28 plus `create_release`, `get_release`, and `approve_release`. The
current S20.5 branch adds seven reconstruction lifecycle tools for 38 total; this interface-ready
branch has not been published as a new version.

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
  deterministic Skill archive, cross-channel source parity, fresh Python 3.12
  wheel/sdist installs, both real Darwin Agent gates, and the exact packed MCPB
  stdio/resource gate passed;
- PyPI 0.6.0 and the GitHub Release are published. The first GitHub Release job
  lacked an explicit repository binding after deliberately omitting checkout;
  the gated assets were recovered from the successful run and published without
  rebuilding, and the workflow now passes `--repo "$GITHUB_REPOSITORY"` for
  future releases.

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

No production ledger, background controller, new validation framework, PR,
release, or deployment is selected. Read-only independent review is limited to
the coherent S10 closeout and creates no persistent validation machinery.
The user's standing publication instruction permits intentional commits and
branch pushes when a coherent verified slice is ready; scope must still be
audited because the worktree contains unrelated untracked paths.

## 6. P2 and WorkBuddy closeout (historical)

The user approved the following product boundary on 2026-08-02:

> P2-A is a rigid 2–10 component assembly MVP using one managed FCStd,
> explicit component identity/placement, per-component verification, and
> interference checks; defer native joints, cross-revision instances, BOM,
> TechDraw, and release packaging to later P2 slices.

P2-S04 and the WorkBuddy compatibility patch are published as VibeCAD 0.6.1 on
PyPI and GitHub. The integration reuses the same
local stdio MCP, Skill, daemon, Task/Revision/Review, and Release authority; no
second control plane or Blob/path adapter was added. The existing `.workbuddy/`
memory directory was not treated as connector configuration and remains
user-owned.

The WorkBuddy 5.3.5 audit and live evidence are recorded in
`docs/WORKBUDDY_COMPATIBILITY_RESEARCH.md`. It confirms host-level local stdio,
strict MCP startup, the 31-tool surface, durable task/restart recovery, exact
Release approval, and native PDF/ZIP Blob persistence. A fresh PyPI 0.6.0
install connected, but the live run found two blockers: WorkBuddy's reserved
`tools/call._meta` was rejected, and the Release drawing's 60-second deadline
exceeded the generic Worker 30-second cap. The v0.6.1 candidate strips bounded
host metadata before dispatch and admits the intended Release deadline.

GLM-5.2 then created, verified, and committed a two-part assembly; resumed it
across separate WorkBuddy processes; approved an exact 45,559-byte Release ZIP;
and read both the 22,372-byte PDF and ZIP through native `ReadMcpResource`.
Revision and HEAD remained unchanged by approval. GLM-5.2 is a provisional
default only with exact task-tool scoping because the run exposed unsafe
runtime-maintenance choices before those tools were excluded. The remaining
comparison matrix is Kimi-K3 (quality ceiling), MiniMax-M3
(cost/performance), and DeepSeek-V4-Flash (fast/economy); `Auto` is excluded from
certification because opaque routing is not reproducible.

The exact local 0.6.1 wheel candidate was then installed into both the isolated
WorkBuddy launcher and managed runtime. A fresh WorkBuddy process reported
version 0.6.1, recovered the approved Release, and re-read the identical ZIP
Blob without repository `PYTHONPATH`. Local release evidence is 5,629 non-slow
tests passed, 114 deselected, full Ruff, version guard, wheel/sdist Python 3.12
fresh installs, Twine, MCPB validation/pack, and independent ZIP integrity.

GitHub Actions run
[`30805731339`](https://github.com/wangtao9090/VibeCAD/actions/runs/30805731339)
passed version guard, quality, package gate, both real managed-agent scenarios,
PyPI, and GitHub Release publication. The public
[v0.6.1 Release](https://github.com/wangtao9090/VibeCAD/releases/tag/v0.6.1)
contains MCPB SHA-256
`faf15e15059e5f186b3c81ed85854a727914eacc9ffafb9ec0c15fa969d0a077`
and Skill SHA-256
`0c2b6c8d72b1654e67fbc12bb7234445c358da95bd4b870f165bc40082b96727`.
The [PyPI 0.6.1](https://pypi.org/project/vibecad/0.6.1/) wheel SHA-256 is
`f414ec955c3b89352cad6490000626c1dcfd34b742befe9b50774c6385fe3407`;
a clean index install reported version 0.6.1, 31 tools, epoch 4, the frozen
public-surface digest, and no local direct-url provenance.
