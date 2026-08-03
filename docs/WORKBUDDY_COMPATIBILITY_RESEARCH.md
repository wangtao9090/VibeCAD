# WorkBuddy compatibility and model-selection research

> Status: VibeCAD stdio registration and connection confirmed; exact tool,
> resource, recovery, and Release certification remains
>
> Evidence date: 2026-08-03
>
> Tested installation: WorkBuddy 5.3.5 on macOS

## Product conclusion

WorkBuddy is a credible first domestic general-purpose host for VibeCAD. The
installed product has native local `stdio` MCP configurations, strict MCP
startup, tool discovery, MCP resource commands, durable task/session storage,
and a local Skill system. VibeCAD should therefore reuse its existing stdio MCP,
host-neutral Skill, daemon, and Task/Revision/Review/Release authority. No new
CAD control plane is justified.

The remaining uncertainty is narrower than the earlier roadmap stated: a real
VibeCAD session must still prove exact JSON Schema behavior, binary
`resources/read` handling for PDF/ZIP payloads, faithful use of
`task_id`/`generation`/`next_action`, and restart recovery. If the release ZIP
cannot be consumed as an MCP Blob, the only permitted fallback is a thin
adapter that saves the already-authorized resource to a user-selected local
path and returns its digest and path.

The current buffered MCP resource contract is intentionally capped at 64 MiB.
P2 rejects a Release package that cannot be retrieved within that contract;
streaming or a larger authenticated local broker remains a later transport
capability rather than an unbounded Blob allocation.

## Host capability matrix

| Required behavior | Evidence | Current status |
|---|---|---|
| Local stdio MCP | WorkBuddy CLI 2.115.0 registered both the source candidate and an exact locally built `vibecad-0.6.0` wheel in isolated environments; both `mcp get` checks reported `Connected` | Confirmed through the exact wheel candidate; published package still pending |
| `tools/list` and strict input schema | The installed CLI is launched with strict MCP configuration and exposes tool-call-capable models | Host-ready; exact VibeCAD 31-tool discovery and invalid-input rejection pending |
| ResourceLink and resource commands | WorkBuddy 5.3.5 contains ResourceLink handling and launches the CLI with `ListMcpResources` and `ReadMcpResource`; the changelog explicitly records MCP Resource support | Confirmed at host implementation level |
| Binary Blob for PDF/ZIP | Blob handling exists in the installed client, but no official statement proves arbitrary MCP binary round-tripping | Must pass a real VibeCAD PDF and approved ZIP test |
| `task_id`, `generation`, `next_action` | These are VibeCAD application semantics, not generic MCP host capabilities | Must be taught by the Skill and checked in a real multi-turn task |
| Restart recovery | Official task documentation supports continuing interrupted tasks; the installed app persists sessions/tasks locally and the changelog records multiple recovery fixes | Host recovery confirmed; exact MCP subprocess and VibeCAD task recovery pending |
| Release draft, digest approval, ZIP read | VibeCAD P2-S04 implements this surface | P2 exit gate passed; live WorkBuddy gate follows publication |

Sources: [WorkBuddy connector documentation](https://www.workbuddy.cn/docs/workbuddy/From-Beginner-to-Expert-Guide/Function-Description/Connector),
[WorkBuddy model configuration](https://www.workbuddy.cn/docs/workbuddy/From-Beginner-to-Expert-Guide/Function-Description/Model),
[WorkBuddy task management](https://www.workbuddy.cn/docs/workbuddy/Task-Management),
[WorkBuddy Skills](https://www.workbuddy.cn/docs/workbuddy/From-Beginner-to-Expert-Guide/Function-Description/Skills-Market),
and [WorkBuddy changelog](https://www.workbuddy.cn/docs/workbuddy/Changelog).

The live registrations used only an isolated project configuration under a
private temporary directory. The wheel check used a dedicated Python 3.12
environment and a private `VIBECAD_HOME`; it did not install or replace the
managed VibeCAD runtime and did not invoke a model. WorkBuddy correctly
required explicit approval before exposing the project-scoped server to a
task; the remaining certification therefore starts at that approval boundary.

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
| Recommended default | **GLM-5.2** | Officially positioned for long-running tasks with a 1M context window; WorkBuddy also recommends GLM for complex multi-step work | Higher listed credit multiplier than the economy candidates; schema fidelity still needs measurement |
| High-quality fallback | **Kimi-K3** | Officially positioned for complex long-horizon autonomous tasks, knowledge work, and research reasoning | Highest listed multiplier in the current official table; do not make it the default without a measured quality gain |
| Cost/performance Agent | **MiniMax-M3** | Officially positioned for code and agent tasks, supports vision, and has a low listed multiplier | Long-run state and exact approval behavior remain unmeasured |
| Fast/economy | **DeepSeek-V4-Flash** | 1M context, speed-first positioning, and the lowest listed non-free multiplier among the main candidates | Speed and context size do not by themselves prove reliable multi-step tool use |
| Connector/debugging | **Kimi-K2.7-Code** | Multimodal and explicitly optimized for programming tasks | Better suited to integration diagnosis than the ordinary CAD-user default |
| Visual diagnosis | **GLM-5V-Turbo** | Native multimodal model for screenshots and UI diagnosis | VibeCAD's normal workflow should rely on structured CAD evidence, not screenshot interpretation |

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
