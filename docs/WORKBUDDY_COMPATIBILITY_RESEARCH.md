# WorkBuddy compatibility and model-selection research

> Status: real GLM-5.2 multi-turn CAD/Release and GLM-5V-Turbo single/multi-view
> image-to-CAD outcome certification completed on macOS; the current Windows
> WorkBuddy host/resource path is also certified with the bounded fixes and
> caveats below
>
> Evidence dates: 2026-08-04 (macOS) and 2026-08-20 (Windows)
>
> Tested installations: WorkBuddy 5.3.5 on macOS; WorkBuddy 5.3.13 and CLI
> 2.115.0 on Windows

## Product conclusion

WorkBuddy is a credible first domestic general-purpose host for VibeCAD. The
installed product has native local `stdio` MCP configurations, strict MCP
startup, tool discovery, MCP resource commands, durable task/session storage,
and a local Skill system. VibeCAD should therefore reuse its existing stdio MCP,
host-neutral Skill, daemon, and Task/Revision/Review/Release authority. No new
CAD control plane is justified.

The real certification closes the host-contract uncertainty. WorkBuddy can
drive the strict VibeCAD tool schemas, retain `task_id`/`generation`/
`next_action`, resume the same durable task in later CLI processes, approve an
exact Release digest, and consume both PDF and ZIP MCP Blobs. WorkBuddy's native
`ReadMcpResource` implementation persists binary Blob content under its
project-scoped `.mcp-resources/` directory and returns that path; VibeCAD does
not need a Resource or arbitrary-path adapter.

One narrow compatibility seam is required for large handwritten ModelPrograms.
Raw stdio capture proved that WorkBuddy delivered 9–11 KiB arguments intact, so
this is not a payload-size or MCP framing problem. Invalid submissions returned
precise VibeCAD domain errors on the wire, but the WorkBuddy agent path retried
them and a later application-backed submit surfaced only generic `-32603` while
leaving the Task unchanged. The `vibecad --workbuddy-submit` adapter therefore
reads exactly one bounded project-local JSON request, reuses the canonical
ModelProgram/ParametricDesignIR validators, and submits through a fresh
`LocalAgentClient`. The Task Kernel still revalidates and remains the only
execution, verification, Revision, review, and HEAD authority. No MCP tool or
second CAD control plane was added.

The certification exposed two defects fixed after the published v0.6.0 tag:

- WorkBuddy adds a reserved `_meta.__session` object to `tools/call` params.
  VibeCAD now admits a bounded plain object or null for this MCP-reserved field
  and strips it before SDK/application dispatch; tool arguments remain exact.
- Release drawing requested the intended 60-second Worker deadline while the
  generic Worker RPC validator capped calls at 30 seconds. The validator now
  admits exactly 60 seconds and still rejects larger deadlines.

The published v0.6.0 package remains the installation and connection baseline,
but the complete live certification used an isolated same-version wheel refresh
containing these fixes. VibeCAD 0.6.1 carries the certified patch.

The current buffered MCP resource contract is intentionally capped at 64 MiB.
P2 rejects a Release package that cannot be retrieved within that contract;
streaming or a larger authenticated local broker remains a later transport
capability rather than an unbounded Blob allocation.

## Windows 5.3.13 host-path certification

The 2026-08-20 Windows run used WorkBuddy 5.3.13, CLI 2.115.0, an isolated
project-local MCP/Skill configuration, a private `VIBECAD_HOME`, and the same
managed FreeCAD 1.1.0 runtime used by VibeCAD 0.10.0. It completed the public
project/task/model path without an alternate CAD authority:

- project `project_93a2360782d22b28e324edb0ed4d9504` and task
  `task_7dc0556a554bb5edba94288333d1afeb` converged at generation 11;
- committed Revision `revision_1b050827ff2e788bd757d518b807ada4`
  became HEAD;
- the editable result was a `12 x 8 x 4 mm` box with volume `384 mm3`, and all
  10 deterministic acceptance checks passed;
- WorkBuddy's native deferred tool/resource path used `ToolSearch`,
  `DeferExecuteTool`, `ReadMcpResource`, and the exact VibeCAD export operation;
- the persisted FCStd was 2,958 bytes with SHA-256
  `21069d6682e5cd67f7137977c2de7e6717896c527dd3861e86fd33bb376b3738`;
- the persisted STEP was 6,836 bytes with SHA-256
  `5cb0c60fc0d145849fc129cd34105aa9ba545221d1ca5e0938333ae5e2ed4a96`.

Independent local reads matched both sizes, hashes, and native file signatures
against the VibeCAD manifest. This certifies the exact Windows host profile and
binary resource flow; it does not imply that every WorkBuddy model or future
release has been certified.

WorkBuddy 5.3.13's strict schema validator does not admit JSON `null` for the
committed-artifact `draft_id` selector even though `null` remains the canonical
MCP value. VibeCAD therefore also admits the exact string `"committed"` at the
public parsing boundary and normalizes it immediately to the existing internal
`None` authority. Draft IDs remain strict `draft_<32 lowercase hex>` values,
unknown strings still fail closed, and the canonical `null` contract remains
unchanged for other hosts. The project's canonical Skill documents this narrow
host compatibility alias.

## Host capability matrix

| Required behavior | Evidence | Current status |
|---|---|---|
| Local stdio MCP | WorkBuddy CLI 2.115.0 registered the source candidate, the exact locally built wheel, and a fresh PyPI install of `vibecad==0.6.0` in isolated environments; every `mcp get` check reported `Connected` | Confirmed through the published package |
| `tools/list` and strict input schema | The published server exposed 31 tools and the current visual branch exposed 38; malformed ModelPrograms failed closed with exact paths before corrected submission | Passed with the `_meta` fix and bounded submit adapter |
| ResourceLink and resource commands | Release preview resources were returned through the existing MCP surface and WorkBuddy read them by URI | Passed |
| Binary Blob for PDF/ZIP | WorkBuddy persisted the 22,372-byte PDF and 45,559-byte ZIP; independent size and SHA-256 checks matched | Passed; no adapter required |
| `task_id`, `generation`, `next_action` | The real task reached `succeeded` at generation 14 with `next_action=none`; Revision and HEAD stayed stable through Release approval | Passed |
| Restart recovery | Separate CLI processes resumed one WorkBuddy session, recovered the durable task and draft Release, and did not replay CAD mutations | Passed |
| Release draft, digest approval, ZIP read | Digest-bound approval advanced only Release generation 0 -> 1 and exposed the immutable ZIP | Passed with the 60-second Worker fix |
| Host-visible image to editable CAD | GLM-5V-Turbo read the installed Skill/reference and dimensioned fixtures, including three complementary orthographic L-bracket views; strict compilation produced editable Pad plus three Hole locations and rejected two ambiguous/conflicting sets before Task creation | Outcome-passed for the frozen single- and multi-view fixtures; the multi-view first handwritten IR needed bounded contract corrections, so this is not a zero-repair or universal-photo claim |

Sources: [WorkBuddy connector documentation](https://www.workbuddy.cn/docs/workbuddy/From-Beginner-to-Expert-Guide/Function-Description/Connector),
[WorkBuddy model configuration](https://www.workbuddy.cn/docs/workbuddy/From-Beginner-to-Expert-Guide/Function-Description/Model),
[WorkBuddy task management](https://www.workbuddy.cn/docs/workbuddy/Task-Management),
[WorkBuddy Skills](https://www.workbuddy.cn/docs/workbuddy/From-Beginner-to-Expert-Guide/Function-Description/Skills-Market),
and [WorkBuddy changelog](https://www.workbuddy.cn/docs/workbuddy/Changelog).

The live registrations used only isolated project configurations under private
temporary directories. The wheel and PyPI checks used dedicated Python 3.12
environments and private `VIBECAD_HOME` roots; they did not install or replace
the user's managed VibeCAD runtime and did not invoke a model. The fresh PyPI
environment reported VibeCAD 0.6.0 with 31 public tools before WorkBuddy
reported `Connected`. WorkBuddy correctly required explicit approval before
exposing the project-scoped server to a task. The live run used the public
`vibecad` supervisor entrypoint, strict project MCP configuration, and the
packaged Skill copied into `.codebuddy/skills/`; the user's unrelated
`.workbuddy/` directory was not used or inspected.

## Live GLM-5.2 certification evidence

The canonical run created and delivered a two-component rigid assembly:

- task `task_e9f9dc52c8f75cd72feddee2648564b8` completed at generation 14;
- Revision `revision_5339b7d4de17feb4a8696427be31240d` became project HEAD;
- all five acceptance criteria passed: two components, no interference,
  complete BOM, two BOM rows, and total quantity two;
- Release `release_86e9eda3cdaf3679b777a4ab3400c5d0` was approved only after
  the user confirmed package SHA-256
  `f4265c063e92cf60823e4b4c2194f93520d83bf525195a4cc4508f04736bf92f`;
- approval advanced Release generation 0 -> 1 without changing Revision, HEAD,
  task ID, or task generation;
- the 45,559-byte ZIP passed independent archive integrity checks and contained
  exactly FCStd, STEP, BOM JSON/CSV, assembly PDF, manifest, and validation
  report entries.

The PDF persisted by WorkBuddy was 22,372 bytes with SHA-256
`f2a03e3589f5af8287305a506151342db073b60314f6d76fdcc39df836be83d3`.
The persisted ZIP had the exact approved package digest above. This proves real
`resources/read` Blob behavior rather than merely installed client code paths.

The final local 0.6.1 wheel candidate had SHA-256
`f414ec955c3b89352cad6490000626c1dcfd34b742befe9b50774c6385fe3407`.
Both the WorkBuddy launcher environment and its isolated managed runtime were
upgraded to that exact wheel without repository `PYTHONPATH`; a new CLI process
reported service version 0.6.1, recovered the approved Release, and persisted
the same 45,559-byte ZIP with the exact approved digest. The repository's final
non-slow gate passed 5,629 tests with 114 deselected; full Ruff, version guard,
wheel/sdist fresh-install, Twine, MCPB validation, and archive integrity gates
also passed locally.

One model-safety limitation remains: during early schema recovery GLM-5.2 twice
confirmed an isolated runtime-uninstall call despite instructions not to alter
the runtime. Only the disposable private runtime was affected and durable task
data survived. Until the wider model matrix is run, use GLM-5.2 only with an
exact task-tool allowlist that excludes runtime maintenance tools.

## Live GLM-5V-Turbo image-to-CAD evidence

The visual certification used WorkBuddy 5.3.5, CLI 2.115.0, the project-scoped
canonical Skill, a private `VIBECAD_HOME`, and the repository-owned 1,200 ×
1,200 single-hole-plate drawing. Permissions were limited to the fixture and
Skill reads, one exact request-file write, one exact adapter command, and six
named MCP operations; broad Bash/Write and runtime-maintenance access were not
granted.

GLM-5V-Turbo correctly read the confirmed `80 × 50 × 8 mm`, centered `Ø10`
through-hole intent and authored a strict ModelProgram with two editable
sketches, Pad, and Through-All Hole. The bounded adapter submitted it to the
ordinary Task Kernel. Task `task_4a5520dd7e3b9289eacf873565f71dd4`
reached generation 9, `awaiting_user_review`, with `last_error=null`; project
HEAD remained at base Revision `revision_7d20d63e0b628c77c2b2aad3091cdcfd`
and the unaccepted candidate was
`revision_d9fd05cc16963d48d080d2009cd395f9`.

All deterministic verdicts passed: bounding box `80 × 50 × 8 mm`, volume
`31371.681469282037 mm³`, valid BRep, and one solid. The draft contains a
16,337-byte FCStd and 8,311-byte STEP artifact. A separate fresh WorkBuddy
process with only `get_task` permission recovered the same generation 9,
candidate Revision, draft, `review_draft` action, and null error without a
mutation. This certifies the frozen
dimensioned fixture and the WorkBuddy host path; it does not certify arbitrary
photos, unknown scale, hidden geometry, or universal visual reconstruction.

### S35 multi-view outcome and authoring caveat

The S35 run gave GLM-5V-Turbo three repository-owned 1,200 × 1,200 orthographic
views and the current canonical Skill/reference. It correctly reconciled the
same-object/state/scale evidence into an L section with `50 × 40 × 60 mm`
overall dimensions, `8 mm` legs, two `Ø6` through holes on the XZ plane, one
`Ø6` through hole on the YZ plane, and expected volume `38681.42 mm³`.

The first handwritten submission was not accepted as a clean host-authored IR.
WorkBuddy first tried the large MCP submission and received the known generic
`-32603`, then wrote the bare ModelProgram instead of the adapter's four-field
request envelope. After mechanical envelope correction, the canonical adapter
reported `/program/operations/0/args/design/features/1/axis`; later deterministic
compilation exposed a redundant L-profile dimension set, a wrong XZ Hole
direction, and a separate radius/diameter identity. Broad Bash/Write permission
for an autonomous repair was denied; the controller changed only those bounded
strict-contract fields and did not grant general shell access.

The corrected request entered a fresh `REQUIRE_REVIEW` Task
`task_d17e24e1cad5f4f67a4c4408975100a7`. At generation 9 it was
`awaiting_user_review`, `next_action=review_draft`, `last_error=null`, with
candidate `revision_a6677b34e81d32284369a3b99e3f7067`; project HEAD remained
the empty base `revision_23bf0ca98a4a7258178ae436943a7e72`. All required
verdicts passed: bbox `[50, 40, 60]`, volume `38681.4159868246 mm³`, valid BRep,
and one solid. The unaccepted draft contains a 24,051-byte FCStd and a
13,715-byte STEP artifact. A separate Read-only GLM-5V-Turbo run returned
`SAFE_FAILURE` for both missing extrusion depth and a front/top `50/45 mm`
width conflict, with the shortest correct clarification for each and no CAD
authority.

A final adversarial Read-only replay caught a fixture-quality issue that the
first successful run had tolerated: the top image used a different printed
scale and the front-view `22/36` dimensions did not name their coordinate
origin clearly. The controller treated this as invalid positive-test evidence,
not as permission for the model to guess. The settled images now use one 4:1
scale and explicit lower-left-origin `(X,Z)` / `(Y,Z)` hole coordinates. A new
GLM-5V-Turbo session, without expected facts in its prompt and with only `Read`,
returned `PASS`, the exact `50 × 40 × 60 mm` envelope, 8 mm legs, all three Ø6
hole centers, no conflicts, and no blocking unknowns. This replay validates the
current image facts only; the verified CAD draft remains the earlier Task and
is not misreported as a second end-to-end write.

This closes the fixed S35 product outcome while preserving an important model
quality distinction: WorkBuddy's visual fact extraction passed, but its first
strict IR authoring attempt did not. The Skill now makes the independent
constraint set, Hole `axis: null`, shared diameter identity, origin-plane
direction, and four-field adapter envelope explicit. Deterministic compilation
and review remain mandatory; this result does not support a zero-repair rate or
ordinary-photo claim.

## Published baseline

VibeCAD 0.6.0 is available on
[PyPI](https://pypi.org/project/vibecad/0.6.0/) and as a
[GitHub Release](https://github.com/wangtao9090/VibeCAD/releases/tag/v0.6.0).
The published wheel SHA-256 is
`10a0e80ac6420219d329f89f3819700c1ad0e767dd8ba1b99ae961d2083fb6d6`.
The GitHub Release carries the gated `VibeCAD.mcpb` and standalone Agent Skill
archive; the latter has SHA-256
`f7e154aa6b8d2eafbd4f8a101aeb870199ff7bcad5ed04c9efd7748b4b97fca4`.

## Published compatibility patch

VibeCAD 0.6.1 is published on
[PyPI](https://pypi.org/project/vibecad/0.6.1/) and as a
[GitHub Release](https://github.com/wangtao9090/VibeCAD/releases/tag/v0.6.1).
The published wheel and sdist SHA-256 values are respectively
`f414ec955c3b89352cad6490000626c1dcfd34b742befe9b50774c6385fe3407`
and `123e9b644f3283d25388533252a6898dcd4574b64dc70507d311c32f0e0c2781`.
The GitHub Release MCPB and standalone Skill archive SHA-256 values are
`faf15e15059e5f186b3c81ed85854a727914eacc9ffafb9ec0c15fa969d0a077`
and `0c2b6c8d72b1654e67fbc12bb7234445c358da95bd4b870f165bc40082b96727`.

The tag workflow passed version, Ruff, 5,629-test non-slow, package/fresh
install, two real managed FreeCAD Agent, packed MCPB resource, PyPI, and GitHub
Release jobs. A separate clean PyPI installation resolved no local
`direct_url`, reported VibeCAD 0.6.1, 31 tools, runtime epoch 4, and the frozen
public-surface digest.

## Models available to the installed WorkBuddy account

The current cloud-resolved CLI allowlist contains these user-selectable models:

- Auto
- Hy3
- GLM-5.2
- GLM-5.1
- GLM-5V-Turbo
- MiniMax-M3
- Kimi-K3
- Kimi-K2.7-Code
- Kimi-K2.6
- DeepSeek-V4-Flash
- DeepSeek-V4-Pro

This list was confirmed from the running 5.3.5 installation rather than inferred
from the older model catalog bundled in the app. Display names above follow the
current official WorkBuddy model documentation; internal resolver IDs are not a
public VibeCAD contract.

## Recommended VibeCAD model profile

| Role | Initial candidate | Why it fits | Limitation before certification |
|---|---|---|---|
| Provisional default | **GLM-5.2** | The canonical real CAD/Release run passed, including restart recovery and Blob reads | Use an exact tool allowlist; the run exposed unsafe runtime-maintenance choices before scoping was narrowed |
| High-quality fallback | **Kimi-K3** | Officially positioned for complex long-horizon autonomous tasks, knowledge work, and research reasoning | Highest listed multiplier in the current official table; do not make it the default without a measured quality gain |
| Cost/performance Agent | **MiniMax-M3** | Officially positioned for code and agent tasks, supports vision, and has a low listed multiplier | Long-run state and exact approval behavior remain unmeasured |
| Fast/economy | **DeepSeek-V4-Flash** | 1M context, speed-first positioning, and the lowest listed non-free multiplier among the main candidates | Speed and context size do not by themselves prove reliable multi-step tool use |
| Connector/debugging | **Kimi-K2.7-Code** | Multimodal and explicitly optimized for programming tasks | Better suited to integration diagnosis than the ordinary CAD-user default |
| Visual image-to-CAD pilot | **GLM-5V-Turbo** | The real fixed-fixture run read the image and Skill, authored strict IR, and reached verified review | Proven for one dimensioned synthetic plate only; deterministic CAD evidence, not visual similarity, decides acceptance |

`Auto` remains a convenience option for casual use, but not for the release
certification matrix: an opaque router makes failures and regressions harder to
reproduce. Hy3 is useful for low-cost exploration while its promotion is active,
but the current product positioning is not enough to select it as VibeCAD's
stable default.

The credit multipliers and vendor positioning change independently of VibeCAD.
Recheck them at release time against the official
[WorkBuddy model table](https://www.workbuddy.cn/docs/workbuddymini/features/Select-Model)
and do not encode prices in the connector or Skill.

## Multimodal boundary for future photo-to-CAD work

Photo-to-CAD requires at least one vision-capable component, but it does not
require the primary orchestration model to be multimodal for every task. Keep
the roles separable: a vision model extracts contours, candidate dimensions,
part relationships, occlusions, and uncertainty into a bounded structured
observation; the ordinary CAD reasoning model converts confirmed observations
into VibeCAD operations and acceptance criteria; deterministic geometry tools
remain the commit authority.

A single photo is not sufficient evidence for scale or hidden geometry. The
workflow must request a known scale anchor and, when necessary, additional or
orthographic views before constructing a sketch. After CAD generation, render
the candidate and use vision only as a qualitative comparison; dimensions,
topology, interference, and artifact checks still decide acceptance.

For this future visual track, test GLM-5V-Turbo first and MiniMax-M3 as the
second candidate. This visual routing is independent of the default long-task
choice and is not part of the P2 product scope.

## Small certification matrix after P2

Avoid a broad model benchmark. Run one canonical VibeCAD delivery task three
times on each of four candidates: GLM-5.2, Kimi-K3, MiniMax-M3, and
DeepSeek-V4-Flash. The task must:

1. discover the complete public tool surface and submit only schema-valid calls;
2. create and finish a durable CAD task while carrying the exact task ID,
   generation, Revision, and `next_action` across turns;
3. resume the same task after a WorkBuddy restart without replaying a completed
   mutation;
4. read the Revision-bound PDF preview through MCP resources;
5. present the exact Release digest for user approval and read/save the approved
   ZIP without changing Revision or HEAD.

Record only end-to-end success, invalid tool calls, unnecessary retries, turns,
elapsed time, and WorkBuddy-reported credit use. Select one default and one
fallback. A model is not `VibeCAD recommended` merely because it appears in the
WorkBuddy selector or succeeds once.

Run one separate visual smoke test with GLM-5V-Turbo and MiniMax-M3: provide a
simple dimensioned reference image, require a structured uncertainty report,
construct the bounded CAD candidate, and compare its render without allowing
the vision verdict to override deterministic acceptance. This smoke test
informs the later photo-to-CAD roadmap and does not expand P2 certification.
