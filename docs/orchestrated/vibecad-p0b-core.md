# VibeCAD P0-B Core Orchestrated Delivery

- Active revision: `P0B-R1.1`
- Revision state: `approved / executing`
- Approved pre-authorization SHA-256:
  `0efcff51f24f983139529212c185adf250fb4b3add37c7dd1f836477e24edc5a`
- Repository: `/Users/wangtao/Documents/DevProject/vibecad`
- Branch: `codex/agent-stage3`
- Planning anchor: `4d8dc88017f658c93cd97c8ee616b9905c3af781`
- Predecessor: `docs/orchestrated/vibecad-agent-stage3.md`, S3-8 completed
- Product boundary: expert Agent, user-provided model, one Task Kernel, controlled
  ModelProgram, Workbench as a client rather than a second authority

This file is the cross-session source of truth for P0-B core. Native plans are
only a projection of this artifact. History, approvals, gate evidence and
recovery snapshots are append-only after approval.

## 1. Context

### User goal

Move VibeCAD from a locally host-ready Agent package to a recoverable product
backend that Claude, Codex and a later FreeCAD Workbench can share. A user must
be able to lose a response, restart a client, find the task again, inspect its
history and artifacts, compare or revert revisions, cancel work, and survive a
FreeCAD hang or crash without corrupting the source file or losing the Kernel.

### Starting point

- VibeCAD `0.5.0`, runtime epoch `4`, 20 public MCP tools.
- Stage 3 / S3-8 is complete at local commits `3b80085` and `4d8dc88`.
- Final Stage 3 gate: `3877 passed, 95 deselected`; real managed-runtime and
  packed MCPB gates passed; independent reviews reported `0/0/0`.
- Task, candidate, revision, artifact and checkout stores are durable and
  bounded, but public discovery and recovery are incomplete.
- `create_task` generates a random task ID and has no caller request key. A
  lost response can therefore orphan a task.
- MCP currently owns an in-process `AgentApplication`; FreeCAD/OCCT calls share
  that process. A native hang cannot be interrupted and a crash can terminate
  the control plane.
- The G0 interaction codec and managed checkout are strict seams, not a
  runnable authenticated daemon or a safe file-delivery product.

### Objective success criteria

P0-B core is complete only when all of the following are true:

1. `create_task` is replay-safe across response loss, concurrency and restart.
2. Projects, tasks, task transitions, committed revision ancestry and artifact
   manifests are publicly discoverable without loading FreeCAD.
3. Revision comparison reports verified ancestry and file/artifact changes;
   it does not mislabel metadata comparison as geometric semantic diff.
4. Cancellation is durable. A stuck FreeCAD call can be terminated and then
   reconciled without replaying an uncertain mutation.
5. Revert creates and verifies a new revision based on current HEAD; HEAD is
   never moved backward and internal rollback cleanup is never exposed.
6. One authenticated local daemon owns the only `AgentApplication` / Task
   Kernel used by MCP and the future Workbench.
7. A session-bound file grant can expose only daemon-created managed checkout
   files. Stale or revoked sources cannot be newly claimed or accepted.
8. FreeCAD runs behind a killable Worker generation. Worker crash, hang, EOF,
   protocol corruption or timeout does not kill the daemon, corrupt HEAD, or
   modify the original user file.
9. The public surface is 28 tools: the existing 20 plus the eight lifecycle
   tools frozen in P0B-D05. Discovery remains at most 32 KiB.
10. Full automated, real managed FreeCAD, packed MCPB and two-client acceptance
    gates pass with independent semantic and adversarial review.

### Known failure modes

- response-loss duplicate/orphan creation;
- partial list results that silently skip corrupt records;
- stale pagination cursors mixing two snapshots;
- direct HEAD rewind or confusing candidate cleanup with product revert;
- treating MCP request cancellation as FreeCAD interruption;
- using the current non-runnable codec identifiers as authentication;
- two processes independently constructing `AgentApplication` over the same
  data root;
- accepting client-provided local paths or dynamically dispatching wire names;
- checkout bytes remaining valid while their HEAD/draft authority is stale;
- FreeCAD C++ hangs, OCCT crashes, response truncation and lost worker replies;
- silently replaying a mutation after the worker generation is uncertain.

### Explicit exclusions

- FreeCAD Qt Workbench UI, TaskPanel and visual diff (G1/G2);
- face/edge Selector Level B, Sketcher, PartDesign and STL-to-STEP (P1);
- geometric/entity semantic diff; P0-B comparison is metadata, ancestry and
  artifact diff only;
- dirty checkout publish, arbitrary local path import, arbitrary Python or
  dynamic wire handlers;
- retention/GC, runner generation migration and complete operational telemetry
  (P0-B hardening);
- Worker pool, remote queue, multi-tenant sandbox and cross-machine execution
  (P3);
- Linux/Windows daemon support in this macOS-first packet;
- push, pull request, tag, release, marketplace publication or external spend.

### Approval source

The user's standing instruction to continue autonomously authorized the
read-only audit and preparation of the repo artifact. After the P0-B product
outcome and S3-8 delivery status were presented, the user explicitly said
“好的 持续推进吧”. P0B-A01 binds that instruction to the independently reviewed
`P0B-R1` digest, decisions, allowlist, gates and exclusions.

## 2. Capability Profile and Adapter

Selected adapter: Codex.

```text
approval: native-plan
delegation: spawn-send-wait
persistence: repo-artifact
process: native-session-poll
```

Permitted capability evidence:

- `live capability declarations`: `update_plan`, `spawn_agent`,
  `send_message`, `followup_task`, `wait_agent`, `exec_command` and
  `write_stdin` are declared in the current session.
- `observable behavior`: three bounded read-only P0-B audits were spawned,
  messaged, waited on and returned results; command sessions and polling have
  been observed in this Codex task.
- `environment identity`: the host identifies this as Codex desktop with the
  VibeCAD workspace rooted at the repository path recorded above.
- `public configuration`: the current host publicly declares unrestricted
  workspace filesystem access, network availability and no command-approval
  escalation path; those capabilities do not authorize external publication.

Capability discovery introduced no network access, installation, task launch
or credential inspection. The artifact remains authoritative if native plan
state diverges.

## 3. Decisions

### Product and lifecycle decisions

- **P0B-D01 — Reliability before CAD breadth.** P0-B adds discovery,
  replay/recovery, comparison, cancellation, revert, daemon/file delivery and
  crash isolation. It does not add new modeling operations.
- **P0B-D02 — One Kernel authority.** One local daemon owns one
  `AgentApplication`. MCP and Workbench are clients. No client, Worker or
  plugin may construct a second Task/Revision write authority.
- **P0B-D03 — Replay-safe task creation.** `create_task` requires
  `create_key` matching `task_create_[0-9a-f]{32}`. The task ID is
  `task_` plus the first 32 lowercase hex characters of
  `SHA-256(b"vibecad-task-create-v1\\0" + UTF8(create_key))`.
  The ID depends only on the key. The complete 256-bit domain-separated key
  digest is persisted with the task so a truncated-ID collision cannot be
  mistaken for replay. Same complete digest plus identical immutable intent
  (`project_id`, external reasoning owner and `review_policy`) returns the
  original task; a different digest or intent at that ID returns `conflict`.
  Legacy random-ID tasks have no creation digest and therefore conflict rather
  than being adopted as a keyed replay.
- **P0B-D04 — Recovery is composition, not another tool.** A lost create reply
  is recovered by replaying `create_task(create_key)`. An existing task is
  recovered through `list_tasks -> get_task -> resume_task`. There is no
  redundant public `recover_task` tool.
- **P0B-D05 — Eight public lifecycle tools.** Add exactly
  `list_tasks`, `get_task_events`, `list_projects`, `list_revisions`,
  `compare_revisions`, `get_artifact_manifest`, `cancel_task` and
  `revert_project`. `compare_revisions` contains the diff result; no separate
  `diff_revisions` tool is added. The public total becomes 28.
- **P0B-D06 — Fail-closed discovery.** Lists sort by canonical ID, return at
  most 50 entries by default and 100 by request, and bind opaque cursors to a
  digest of the complete validated snapshot. Snapshot change returns
  `conflict`. Unknown, corrupt, unsafe or inconsistent records fail the whole
  request; partial success is forbidden. `list_revisions` returns only the
  validated complete ancestry of current committed HEAD. Drafts/candidates and
  unreachable or abandoned revisions are not presented as committed history;
  a cycle, fork ambiguity or missing base fails closed.
- **P0B-D07 — Task events reuse authority.** `get_task_events` is a bounded,
  ordered projection of the persisted `TaskRun.transitions`. P0-B core does
  not create a second event store or claim timestamps/telemetry that do not
  exist.
- **P0B-D08 — Honest revision comparison.** Comparison covers project identity,
  ancestor relation, base change, revision manifest, model/STEP presence,
  hashes, sizes and artifact-set changes. Geometry/entity/parameter diff is
  explicitly `unsupported` and remains P1/G2 work.
- **P0B-D08A — Read-only artifact manifest.** `get_artifact_manifest` returns
  exact task/generation/revision/draft binding, revision-manifest digest,
  verification/observation digests, and artifact format/hash/size. If a
  delivery is already materialized it also returns its resource URI and
  delivery-manifest digest; otherwise it returns `materialized=false` and
  performs no export, copy or materialization side effect.
- **P0B-D09 — Verified forward revert.** `revert_project` binds a source
  revision and expected current HEAD, copies the historical sealed payload to
  a new candidate based on current HEAD, reloads and verifies it, and uses the
  normal review/CAS path to publish a new revision. It never rewrites HEAD and
  never exposes `rollback_revision`. Its public request is exactly
  `schema_version`, `revert_key`, `project_id`, `source_revision` and
  `expected_head`; `revert_key` matches `revert_create_[0-9a-f]{32}`. A
  domain-separated complete key digest and deterministic task ID bind that key
  to the other four immutable fields. The operation always creates or replays
  one `require_review` TaskRun and returns its ordinary task mapping, normally
  at `awaiting_user_review`; acceptance still uses `accept_draft`. Same key and
  intent returns the existing TaskRun at its current generation, while any
  changed intent conflicts. Response loss therefore cannot create a second
  revert task or commit.
- **P0B-D10 — Durable cancellation.** Add explicit cancel-request/cancelling/
  cancelled state semantics. Idle pre-candidate states cancel by CAS. Review
  drafts continue to use reject. Active CAD cancellation persists intent,
  terminates the Worker generation, then reaches `cancelled` only after an
  uncommitted outcome is proven; uncertainty reaches `recovery_required`.
  No mutating job is transparently replayed. C05 initially permits immediate
  public cancel only from `created`, `needs_plan`, `program_ready` and
  `needs_input`; `awaiting_user_review` must use reject, `cancelled` replay is
  idempotent, other terminal states conflict, and active states fail closed
  until C12 connects the persisted request to the kill/reconcile path.

### Daemon, file and Worker decisions

- **P0B-D11 — New protocol, not upgraded fiction.** Preserve the current G0
  non-runnable codec. Add protocol v2 with bounded length framing, a closed
  static dispatcher and explicit version/error contracts. No wire `getattr`,
  callable, Python name, environment variable, internal root or arbitrary path
  is accepted.
- **P0B-D12 — macOS same-user daemon boundary.** The endpoint is an AF_UNIX
  socket under an identity-pinned `0700` run root, with `0600` socket/receipt,
  a single-instance lock, daemon-instance receipt, peer-euid verification and
  boot-secret challenge. Sessions are server-generated and connection-bound.
  Connection count, in-flight requests, frame bytes and idle time are bounded.
  This excludes other OS users and stale endpoints; it does not claim defense
  after a malicious process already controls the same UID.
- **P0B-D13 — Authentication POC veto.** Before daemon implementation, a
  minimal isolated temporary-root POC, with no persistent product-state
  mutation, must prove reliable peer-euid observation on the declared
  macOS/Python runtime, endpoint replacement rejection and concurrent
  single-instance behavior. If peer identity cannot be observed, daemon
  implementation stops for a revised approval; it does not silently weaken to
  secret-only authentication.
- **P0B-D14 — Session-bound grants.** `checkout.open` remains path-free and can
  mint a `grant_id` bound to daemon instance, connection session, checkout,
  source revision/generation, model identity/digest, purpose and expiry. Only
  `file_grant.claim` may reveal the daemon-created managed checkout path.
  Clients never submit a path. Guess, replay, cross-session, expiry, close,
  stale source or revoked draft all fail closed.
- **P0B-D15 — Source liveness.** Re-evaluate authority on every checkout get,
  grant claim and accept: unchanged source is `live`; advanced HEAD is
  `stale`; changed/accepted/rejected draft is `revoked`; indeterminate store
  truth is `recovery_required`. Historical bytes may remain viewable after a
  prior claim, but stale/revoked sources cannot receive a new grant, drive a
  selector or be accepted.
- **P0B-D16 — Parent-owned truth, Worker-owned CAD.** TaskService,
  CandidateCoordinator, stores, leases, journals, review and HEAD CAS remain in
  the daemon. One child Worker generation owns FreeCAD/OCCT and opaque CAD
  sessions, implements the existing CAD ports through a strict private proxy,
  and is told by the protocol only about parent-reserved candidate staging. It
  receives no Task/Revision authority and cannot commit a revision through the
  protocol. Because it is a trusted same-UID child without an OS sandbox, this
  is a capability/protocol and fault-containment guarantee, not a claim that a
  malicious Worker process is unable to open other filesystem paths.
- **P0B-D17 — Kill the generation on uncertainty.** CAD concurrency remains
  one and cached project sessions remain at most four. Parent watchdogs enforce
  one deadline per private CAD RPC, not one 30-second deadline for an entire
  ModelProgram: session open/load, checkpoint, reload, STEP export and evidence
  each receive 30 seconds; each command receives its frozen operation
  `max_runtime_ms` (never above 30 seconds); close receives 5 seconds. No fixed
  whole-task number is claimed because create/accept/revert and recovery paths
  dispatch different reload/close sequences. Each path's test records its exact
  RPC trace and proves that every dispatched call is bounded; the path budget
  is the sum of those observed per-RPC deadlines. Progress is observed only at
  RPC boundaries; a C++ hang within one RPC is killed at that RPC's deadline.
  Timeout, signal, EOF, malformed or oversized response, or generation mismatch
  kills the process group, invalidates every session token in that generation
  and triggers durable rollback/reconcile. Normal typed CAD failures use the
  existing failure path.
- **P0B-D17A — Fresh Worker launch.** Start the managed-runtime interpreter as
  a fresh subprocess/process group with `close_fds=True`; inherit only the
  explicit protocol pipes/descriptors. Never fork a daemon that has threads or
  FreeCAD loaded, never pass the daemon auth secret, data-root handle, store
  descriptor or lease descriptor, and import FreeCAD only inside the child.
- **P0B-D18 — One consumer contract.** After daemon adoption, MCP becomes a
  thin client for application operations. A fake Workbench client imports only
  the public local client package. Both must observe the same task, draft and
  HEAD through one daemon; client EOF must not terminate the daemon.

### Delivery decisions

- **P0B-D19 — Core/hardening boundary.** Core includes lifecycle discovery,
  replay, manifest, compare, cancel/revert, daemon/auth, liveness/grants,
  Worker isolation and two-client E2E. Retention/GC, runner generation
  migration, full operations telemetry and OS sandboxing stay in named
  residuals. P0-B hardening may overlap G1 UI but must close before P1 release.
- **P0B-D20 — Version.** P0-B core packages as VibeCAD `0.6.0`; runtime epoch
  remains `4` unless a separately evidenced dependency/identity incompatibility
  requires a revision. No compatibility shim preserves the old keyless
  `create_task`, because there are no production users and silent fallback
  would preserve the orphan defect.
- **P0B-D21 — Review repair authority.** Approval of `P0B-R1` authorizes the
  controller to close test, static-analysis and independent-review findings
  inside these decisions and allowlists without asking the user again. A new
  approval is required only for a product-boundary change, allowlist expansion,
  weakened gate, irreversible action, external publication or exhausted stage
  budget.
- **P0B-D22 — No publication authority.** Commits remain local and individually
  revertible. Push, PR, tag and release require separate explicit authority.
  This is the explicit `OE-DEV-01` deviation from orchestrated-execution's
  per-commit immediate-push rule: the Skill cannot grant remote mutation that
  the user has not authorized. Every ledger row must say `not-pushed`, preserve
  the exact local commit hash on the non-force branch, pass named-file staging
  and `git fsck --no-dangling`, and keep the append-only recovery snapshot.
  Local stage closeout is allowed but must state that remote backup/publication
  is incomplete. The deviation closes only after explicit push authority and
  observable remote equality.
- **P0B-D22-R1 — Non-force branch push authorized.** P0B-A02 supersedes
  P0B-D22 only for non-force pushes of the current P0-B branch to `origin`.
  Every accepted semantic/docs commit is pushed immediately and remote equality
  is verified before its dependent packet starts. PR creation, tag, release,
  marketplace publication, force-push and external spend remain unauthorized.
  `OE-DEV-01` closes when the first push establishes the upstream and continues
  to be checked by per-commit local/remote equality.

## 4. Commit Sequence

The plan permits 14 semantic commits plus one authorization/plan commit and one
closeout/evidence commit. Up to four additional repair commits may close
in-scope gate or review findings under P0B-D21; the hard stage limit is 20
commits. Every implementation commit gets a genuine focused RED where behavior
changes, focused GREEN, Ruff/diff checks, an independent read-only review and
an append-only ledger row before the next dependent packet.

| ID | Prewritten commit | Exact scope | Independent gate |
|---|---|---|---|
| P0B-C00 | `docs(orchestration): authorize P0-B core delivery` | This artifact and native-plan projection only | artifact contains all eight stage elements, approval quote/revision, clean allowlist/diff |
| P0B-C01 | `feat(tasks): make task creation replay-safe` | deterministic task ID, immutable-intent replay/conflict, store/catalog/API tests; no public list yet | response-loss/restart and concurrent same-key RED/GREEN; task/store/API focused suites |
| P0B-C02 | `feat(tasks): expose bounded task discovery and events` | fail-closed scan, snapshot cursor, task/event projections, Application/MCP schemas | corruption/path/journal/cursor/no-CAD tests; public surface count becomes 22 at this anchor |
| P0B-C03 | `feat(revisions): expose project and revision discovery` | validated project scan and HEAD ancestry list | fork/cycle/missing-base/corruption and stable-pagination tests; no FreeCAD import |
| P0B-C04 | `feat(artifacts): expose revision comparison and manifests` | metadata/ancestry/artifact compare and read-only delivery manifest projection | tamper, missing artifact, no-materialization and honest unsupported-semantic-diff tests |
| P0B-C05 | `feat(tasks): add durable cancellation contracts` | TaskRun cancel states/events, idle CAS cancel and active cancel request contract | exhaustive state matrix, concurrency/restart tests; review drafts remain reject-only |
| P0B-C06 | `feat(revisions): add verified forward revert` | keyed/replay-safe source binding, current-HEAD candidate copy, reload/verify/review/CAS workflow | lost reply replays one TaskRun; old HEAD never restored in place; stale expected HEAD conflicts; source file unchanged |
| P0B-C07 | `feat(interaction): define authenticated local protocol v2` | peer-euid POC, framing, handshake, session/error and closed-dispatch contracts | POC GO plus duplicate/oversized/truncated/replay/unknown-method negative matrix |
| P0B-C08 | `feat(interaction): enforce checkout source liveness` | live/stale/revoked/recovery state recomputation and acceptance guards | HEAD/draft advance, accept/reject, restart and store-integrity matrix |
| P0B-C09 | `feat(daemon): add the single-instance kernel service` | AF_UNIX lifecycle, pinned root/receipt, peer+secret auth, bounded connections and static Application facade | real same-user auth; bad proof/session/receipt/root swap and double-start fail closed |
| P0B-C10 | `feat(interaction): add session-bound file grants` | grant mint/claim/expiry/revoke/close and managed-path broker | guess/replay/cross-session/symlink/hardlink/inode-swap/path injection matrix |
| P0B-C11 | `feat(worker): isolate FreeCAD in a killable generation` | private Worker codec, spawn/process group, opaque session proxy, watchdog, generation fencing | fake exit/hang/corrupt-frame matrix plus real FreeCAD load/modify/checkpoint/export smoke |
| P0B-C12 | `feat(kernel): reconcile worker loss and active cancellation` | Worker loss mapping, full-generation eviction, durable cancel/rollback/recovery integration | real kill at CAD windows; no orphan/source change; precise failed/recovery/cancelled states |
| P0B-C13 | `feat(agent): route MCP and Workbench clients through one kernel` | thin MCP application client, daemon bootstrap/connection, fake Workbench public client | two-client draft preview/hash/accept/reject E2E; EOF/restart share same durable truth |
| P0B-C14 | `chore(release): package P0-B core as 0.6.0` | 28-tool manifest, skill/README/architecture/roadmap, package and managed-runtime refresh evidence | discovery <=32 KiB; full suite; real managed FreeCAD; wheel/sdist/MCPB/fresh-install E2E |
| P0B-C15 | `docs(orchestration): close P0-B core delivery` | final ledger, residuals, hashes and recovery snapshot only | exact-diff review, clean tree, all planned entries terminal, no unauthorized publication |

Dependency order is C01 -> C02; C03 and C04 are sequential; C05 precedes
C12; C07 precedes C09; C08 plus C09 precede C10; C09 plus C11 precede C12;
C02/C04/C06/C10/C12 all precede C13. Read-only design/review work may run in
parallel, but implementation packets sharing files are serialized.

## 5. Manual Validation Matrix

| ID | Environment | Scenario | Expected observation | Owner / user presence |
|---|---|---|---|---|
| P0B-M01 | temporary local data root | create response is discarded, process restarts, same key is replayed | exactly one task ID and generation-zero record are recovered | controller; user not required |
| P0B-M02 | current macOS Python | daemon starts twice and peers authenticate | one daemon wins; valid same-user peer connects; invalid/stale endpoint fails | controller; user not required |
| P0B-M03 | managed FreeCAD 1.1.0 | Worker hangs/crashes during load, mutation, checkpoint, STEP export and evidence | daemon survives, process group dies, no source/HEAD corruption, next generation succeeds | controller; user not required |
| P0B-M04 | MCPB fresh unpack | MCP and fake Workbench review one draft | both see identical revision/hash/verdict; one accept/reject is durable to both | controller; user not required |
| P0B-M05 | installed 0.6.0 candidate | refresh server wheel while preserving runtime and data | FreeCAD core/epoch stay valid and all project/task/revision bytes remain unchanged | controller; user not required |
| P0B-M06 | real Claude/Codex host | install/activate packaged skill and exercise file UX | truthful host workflow succeeds | deferred residual S3-RES-06; separate authorization if host mutation is needed |

There is no UI correctness claim in this stage, so no user-present visual gate
is required. G1 adds that gate when a real Workbench exists.

## 6. Budgets and Circuit Breakers

### Fixed budgets

- stage: 16 planned commits, at most 4 in-scope repair commits, hard maximum 20;
- public tools: exactly 28 at closeout;
- tool discovery JSON: at most 32,768 bytes;
- task/project/revision page: default 50, requested maximum 100;
- CAD concurrency: 1; cached Worker project sessions: 4;
- CAD deadline: per-RPC values in P0B-D17; the whole program is not subject to
  a misleading single 30-second timer;
- ModelProgram: existing 64 commands / 512 KiB admission bounds;
- Worker response: existing logical result maximum 256 KiB;
- model/artifact storage: existing 512 MiB per file and 1 GiB revision bounds;
- daemon endpoint: fixed connection, in-flight, frame and idle limits frozen by
  C07 tests before C09 implementation;
- three implementation GREEN cycles per packet before internal packet replan;
  repair remains inside the total commit budget and P0B-D21.

### Circuit breakers

Freeze the affected packet and append evidence when any of these is observed:

1. unexpected baseline/gate red or a RED that fails for setup/syntax/wrong path;
2. an out-of-allowlist write, unapproved product expansion or weakened gate;
3. peer-euid cannot be proven on the declared runtime (P0B-D13 veto);
4. two live Kernel authorities can write the same data root;
5. a client can submit or escape to an arbitrary local path;
6. corrupt storage yields a partial list rather than a closed failure;
7. stale/revoked checkout can mint/claim a grant or accept a draft;
8. Worker uncertainty causes transparent mutation replay, HEAD change, source
   mutation, orphan process or daemon death;
9. discovery exceeds 32 KiB, the public tool set differs from 28, or a package
   surface differs across checkout/wheel/sdist/MCPB/fresh install;
10. commit 20 would be exceeded, publication becomes necessary, or a decision
    would change expert-Agent/BYO-model/single-Kernel/Workbench-client scope.

Items 1-9 may be repaired autonomously only when the repair stays within the
approved decisions, allowlist and total budget. Item 10, an authentication POC
NO-GO, or any material product-boundary change returns to the user with the
user-visible consequence and choices; internal finding IDs alone are not an
approval request.

## 7. File Allowlist

This stage may modify only the following. Each task packet narrows this list to
exact named files before editing.

### Product source

- `src/vibecad/__init__.py`
- `src/vibecad/server.py`
- `src/vibecad/supervisor.py`
- `src/vibecad/mcp_transport.py`
- `src/vibecad/application/`
- `src/vibecad/workflow/`
- `src/vibecad/execution/`
- `src/vibecad/interaction/`
- new `src/vibecad/daemon/`
- new `src/vibecad/worker/`

### Tests and delivery metadata

- existing `tests/test_agent_application.py`
- existing `tests/test_artifact_materialization.py`
- existing `tests/test_cad_execution_port.py`
- existing `tests/test_candidate_revision.py`
- existing `tests/test_interaction_protocol.py`
- existing `tests/test_managed_checkout.py`
- existing `tests/test_project_api.py`
- existing `tests/test_revision_store.py`
- existing `tests/test_task_api.py`
- existing `tests/test_task_catalog.py`
- existing `tests/test_task_kernel_integration.py`
- existing `tests/test_task_service.py`
- existing `tests/test_task_state.py`
- existing `tests/test_task_store.py`
- existing server, supervisor, MCP transport, runtime, release and package tests
- `tests/fake_server.py`
- `tests/test_agent_skill.py`
- `tests/test_installer.py`
- `tests/test_mcp_transport.py`
- `tests/test_mcpb_manifest.py`
- `tests/test_release_workflow.py`
- `tests/test_runtime_integration.py`
- `tests/test_runtime_purity.py`
- `tests/test_server_agent_surface.py`
- `tests/test_server_new_tools.py`
- `tests/test_server_round5.py`
- `tests/test_server_round6.py`
- `tests/test_server_round7.py`
- `tests/test_server_round8.py`
- `tests/test_server_round10.py`
- `tests/test_server_round11.py`
- `tests/test_server_tools.py`
- `tests/test_supervisor.py`
- new `tests/test_task_discovery.py`
- new `tests/test_revision_discovery.py`
- new `tests/test_revision_compare.py`
- new `tests/test_project_revert.py`
- new `tests/test_local_daemon.py`
- new `tests/test_file_grants.py`
- new `tests/test_freecad_worker.py`
- new `tests/test_p0b_acceptance.py`
- `pyproject.toml`
- `manifest.json`
- `README.md`
- `skills/vibecad-agent/SKILL.md`
- `skills/vibecad-agent/agents/openai.yaml`

### Architecture and orchestration

- `docs/ARCHITECTURE.md`
- `docs/AGENT_ARCHITECTURE.md`
- `docs/PRODUCT_CAPABILITY_ROADMAP.md`
- `docs/orchestrated/vibecad-p0b-core.md`

Generated build outputs may be created only in ignored `dist/`, temporary test
roots or explicitly recorded audit directories. Runtime installation may touch
the existing VibeCAD managed runtime root only during C14's declared
preservation gate. User project/source locations are read-only inputs and may
never be overwritten.

## 8. Expected Impact

### Interfaces and behavior

- `create_task` intentionally becomes key-required; old callers receive a
  stable missing-field error rather than random-ID fallback.
- Public MCP tool count changes from 20 to 28. Existing 20 names remain, but
  their application path is routed through the single daemon by C13.
- TaskRun gains cancellation state/history. Existing stored tasks continue to
  decode; new values are emitted only by 0.6.0 logic.
- Checkout descriptors gain liveness/grant projections without exposing an
  internal path in ordinary list/get responses.
- The current G0 codec stays honest and unchanged in meaning; protocol v2 is a
  separate runnable transport contract.
- Runtime epoch is expected to remain 4; package version and server wheel
  become 0.6.0.

### Tests and performance

- Current full-suite baseline is expected to remain green while focused test
  counts increase.
- Read-only discovery must not import FreeCAD, construct a runtime or acquire a
  project write lease.
- Warm daemon ping/capability traffic is expected to remain sub-100 ms on the
  current host; cold Worker readiness must complete within 15 seconds in the
  managed environment. These are acceptance bounds, not cross-machine SLAs.
- Timeout termination must finish process-group cleanup within 5 seconds after
  the applicable P0B-D17 RPC deadline and leave no observable child process.
- Package contents, version, 28-tool surface and skill wording must be identical
  across checkout, wheel, sdist, MCPB and fresh install.

### Required gate families

- G1 focused: genuine RED/GREEN plus exact affected suites and Ruff/diff.
- G2 integrated: task/revision/artifact/daemon/Worker integration and build.
- G3 environment: actual AF_UNIX peer auth and managed FreeCAD crash/hang tests.
- G4 acceptance: fresh packed MCPB, MCP plus fake Workbench, data-preserving
  refresh and independent semantic/adversarial reviews.

An unexpected full-suite change, performance-bound miss or environment-only
failure is evidence, not permission to loosen the contract.

### Canonical gate commands

Each packet records its exact narrowed file list. The focused gate for every
commit references only files that exist by that commit:

```text
P0B-C01  .venv/bin/python -m pytest -q tests/test_task_state.py tests/test_task_store.py tests/test_task_catalog.py tests/test_task_api.py
P0B-C02  .venv/bin/python -m pytest -q tests/test_task_state.py tests/test_task_store.py tests/test_task_catalog.py tests/test_task_api.py tests/test_task_discovery.py tests/test_server_agent_surface.py
P0B-C03  .venv/bin/python -m pytest -q tests/test_project_api.py tests/test_revision_store.py tests/test_revision_discovery.py
P0B-C04  .venv/bin/python -m pytest -q tests/test_revision_store.py tests/test_revision_compare.py tests/test_artifact_materialization.py
P0B-C05  .venv/bin/python -m pytest -q tests/test_task_state.py tests/test_task_catalog.py tests/test_task_api.py
P0B-C06  .venv/bin/python -m pytest -q tests/test_task_service.py tests/test_revision_store.py tests/test_project_revert.py
P0B-C07  .venv/bin/python -m pytest -q tests/test_interaction_protocol.py tests/test_local_daemon.py
P0B-C08  .venv/bin/python -m pytest -q tests/test_managed_checkout.py
P0B-C09  .venv/bin/python -m pytest -q tests/test_interaction_protocol.py tests/test_local_daemon.py
P0B-C10  .venv/bin/python -m pytest -q tests/test_managed_checkout.py tests/test_local_daemon.py tests/test_file_grants.py
P0B-C11  .venv/bin/python -m pytest -q tests/test_cad_execution_port.py tests/test_candidate_revision.py tests/test_freecad_worker.py
P0B-C12  .venv/bin/python -m pytest -q tests/test_task_state.py tests/test_task_service.py tests/test_task_kernel_integration.py tests/test_freecad_worker.py
P0B-C13  .venv/bin/python -m pytest -q tests/test_interaction_protocol.py tests/test_local_daemon.py tests/test_mcp_transport.py tests/test_server_agent_surface.py tests/test_server_new_tools.py tests/test_p0b_acceptance.py
P0B-C14  .venv/bin/python -m pytest -q
P0B-SLOW .venv/bin/python -m pytest -q -m slow tests/test_freecad_worker.py tests/test_p0b_acceptance.py
P0B-RUFF .venv/bin/python -m ruff check src/vibecad tests
P0B-FMT  .venv/bin/python -m ruff format --check src/vibecad tests
P0B-DIFF git diff --check
P0B-GIT  git fsck --no-dangling
```

Every source packet also runs P0B-RUFF, P0B-FMT, P0B-DIFF and P0B-GIT. C14
adds P0B-SLOW plus the repository's existing build, MCPB validation/pack,
fresh-install and managed-runtime preservation commands, whose exact executable
paths and artifact hashes are appended before C14 starts. C15 runs P0B-DIFF,
P0B-GIT and exact artifact/ledger readback. The slow gate runs only after its
tests exist and the managed FreeCAD environment identity has been recorded;
setup failure is not a passing RED.

P0B-GATE-CORR-01 / 2026-07-23T03:31Z: the first C01 gate invocation without
`PYTHONPATH` collected with four `ModuleNotFoundError` errors because this
Python 3.13.14 environment skips Hatch's hidden editable `.pth`. This is the
already documented repository toolchain behavior, not a semantic RED. For
P0B-C01 through C14 and P0B-SLOW, the exact executable gate is the command
printed above with `PYTHONPATH=src` prepended. Ruff, diff and Git gates are
unchanged. The invalid collection run is preserved in P0B-E03 and must never be
counted as RED or GREEN.

## 9. Residuals

| ID | Evidence / impact | Owner and disposition | Observable closure condition |
|---|---|---|---|
| P0B-RES-01 | Real Claude/Codex host activation is not yet exercised | host integration; preserve S3-RES-06 | authorized real-host install and canonical workflow/file UX pass |
| P0B-RES-02 | Linux/Windows daemon, peer identity and process-group semantics are unproved | later platform packet | native CI/host matrices pass or platform is explicitly unsupported |
| P0B-RES-03 | Same-UID malicious process is outside local daemon threat boundary | P3/security hardening | stronger identity/sandbox design and adversarial host gate |
| P0B-RES-04 | Already opened FCStd bytes cannot be erased from FreeCAD memory | documented G1 behavior | UI reflects revocation and blocks every later claim/accept |
| P0B-RES-05 | Geometric/entity/parameter semantic diff is absent | P1/G2 | two-revision FCStd observation diff passes semantic fixtures |
| P0B-RES-06 | Four-store retention/GC is absent | P0-B hardening | cross-store mark/quarantine/sweep crash matrix passes |
| P0B-RES-07 | Private runner cannot migrate to a new digest in place | P0-B hardening | digest-versioned atomic generation migration passes interruption tests |
| P0B-RES-08 | Complete durable operational telemetry/recovery audit is absent | P0-B hardening | versioned events and recovery matrix accepted |
| P0B-RES-09 | MCP ResourceLink still buffers and caps remote artifact reads at 64 MiB | G1/P1 transport | authenticated streaming/chunk/broker cross-host E2E passes |
| P0B-RES-10 | Worker is fault isolation, not a malicious-code OS sandbox or remote pool | P3 | sandbox/queue/claim/dead-letter and multi-tenant gates pass |
| P0B-RES-11 | Qt Workbench does not exist in P0-B core | G1 | Dock preview/verdict/stale/revoked/Accept/Reject visual acceptance passes |
| P0B-RES-12 | No push, PR, tag or release is authorized | user/publication | explicit publication authority plus remote/release gates |

P0B-RES-12 update at P0B-R1.1: P0B-A02 closes the branch-push portion once
remote equality is observed. PR, tag, release and marketplace publication
remain residual and unauthorized.

## 10. Authorization History

- **P0B-A00 — planning only.** The user's standing instruction to continue
  autonomously supported the read-only audit and this draft. It is not an
  implementation approval because it predates `P0B-R1`.
- **P0B-A01 — approved at 2026-07-23T03:27:51Z.** Exact user wording:

  ```text
  好的 持续推进吧
  ```

  The instruction followed the user-visible explanations of P0-B and the S3-8
  delivery boundary. Before implementation, the controller explicitly told the
  user it would record this wording as the P0-B product-level continuation
  authority, close the five independent plan-review clarifications, freeze
  P0B-R1, and continue without repeated in-scope approvals. It binds approved
  pre-authorization SHA-256
  `0efcff51f24f983139529212c185adf250fb4b3add37c7dd1f836477e24edc5a`,
  P0B-D01 through P0B-D22 including P0B-D08A and P0B-D17A, C00 through C15,
  the stage allowlist, gates, budgets, exclusions, `OE-DEV-01`, and the
  local-commit/no-publication policy.
- **P0B-A02 — approved at 2026-07-23T03:29:39Z.** Exact user wording:

  ```text
  你改 push 的还是 push 哈
  ```

  The controller confirmed this as continuing non-force push authority for the
  current remote branch after every accepted commit. This creates P0B-R1.1 and
  P0B-D22-R1. It does not authorize a PR, tag, release, marketplace publication,
  force-push or external spend.

## 11. Ledger

| Entry ID | Decision / approval | Commit / push | Gate evidence | Residual | Snapshot | State |
|---|---|---|---|---|---|---|
| P0B-E00 / 2026-07-22 | draft P0B-R1; no implementation approval | not-created / not-pushed | three independent read-only audits; anchor/status/allowlist inspection; no source edit | P0B-RES-01..12 | P0B-S00 | draft |
| P0B-E01 / 2026-07-23T03:27:51Z | P0B-D01..D22 plus D08A/D17A at P0B-R1; P0B-A01 | not-created / not-pushed | five pre-approval findings closed; final independent review `0/0/0`; approved digest recorded | P0B-RES-01..12; OE-DEV-01 | P0B-S01 | approved |
| P0B-E02 / 2026-07-23T03:29:39Z | P0B-D22-R1 at P0B-R1.1; P0B-A02 | `6eb209d` plus this authority commit / push pending | exact branch/HEAD/status verified before amendment | P0B-RES-12 narrowed to PR/tag/release; OE-DEV-01 pending first equality | P0B-S02 | authorized-push |
| P0B-E03 / 2026-07-23T03:31Z | P0B-R1.1; C01 gate setup correction under P0B-D21 | not-created / `a7e6881` already pushed and equal | raw C01 command: exit 2, four collection errors from skipped hidden `.pth`; corrected `PYTHONPATH=src` 5-test semantic RED: exit 1, 5 intended failures | known Python 3.13 editable-path residual; no product-scope change | P0B-S03 | superseded-gate |
| P0B-E04 / 2026-07-23T04:23:04Z | P0B-C01 under P0B-R1.1/A01/A02; P0B-D21 allowlist repair | this C01 commit / non-force push required | semantic RED 5/5; focused `1025 passed`; affected integration `229 passed, 19 deselected`; full `3902 passed, 95 deselected`; same-key stress `200/200 + 100/100`; Ruff/diff clean; independent review `0/0/0` | Python 3.13 explicit `PYTHONPATH=src` remains; one existing macOS fork deprecation warning; no product residual | P0B-S04 | accepted-green |

## 12. Recovery Snapshot P0B-S00

### 1. Completed milestones

- S3-8 completed at local HEAD `4d8dc88017f658c93cd97c8ee616b9905c3af781`.
- Three P0-B audits converged and draft P0B-R1 was created; implementation was
  not yet authorized.

### 2. Next steps

1. Close independent plan-review findings.
2. Present the P0-B product outcome and bind explicit user authority to the
   reviewed artifact before implementation.

### 3. Approved decisions

- None for P0-B implementation at this snapshot; P0B-A00 covered planning only.

### 4. Execution discipline

- Draft capability profile was `native-plan / spawn-send-wait / repo-artifact /
  native-session-poll`; no implementation file was permitted to change.

## 13. Recovery Snapshot P0B-S01

### 1. Completed milestones

- Stage 3/S3-8 completed locally at `4d8dc88017f658c93cd97c8ee616b9905c3af781`.
- Predecessor commits: `3b8008560b1865bae6210ecd2e3fe5e2f915f5ee`
  and `4d8dc88017f658c93cd97c8ee616b9905c3af781`; neither is pushed.
- Stage 3 final evidence: 3877 passed / 95 deselected, real managed FreeCAD,
  packed MCPB and independent `0/0/0` reviews.
- P0-B API, daemon/file and Worker/reliability audits completed read-only.
- Artifact revision `P0B-R1` is approved under P0B-A01 at pre-authorization
  SHA-256 `0efcff51f24f983139529212c185adf250fb4b3add37c7dd1f836477e24edc5a`.
- Independent pre-approval review closed five findings and returned final
  `Critical 0 / Important 0 / Minor 0`.
- The working tree contained no pre-existing changes before this artifact was
  created.

### 2. Next steps

1. Commit P0B-C00 locally, verify the anchor and issue the seven-section C01
   packet without requesting P0B-A01 again.
2. If the user changes scope, append P0B-R2 and reopen approval before expanded
   implementation.
3. During C07, if the peer-identity POC is GO, continue C08-C14; if NO-GO,
   append blocked evidence and return with the authentication consequence.
4. For any in-scope gate/review finding, repair under P0B-D21 without asking
   the user again; if scope/authority/budget changes, reopen the approval gate.

### 3. Approved decisions

- P0B-D01..P0B-D22 including P0B-D08A/D17A are approved at P0B-R1 under
  P0B-A01. Do not request the same approval again.
- P0B-A00 covers planning only; P0B-A01 is the implementation authority and is
  distinct from earlier Task Kernel or Stage 3 approvals.

### 4. Execution discipline

- Capability profile: `native-plan / spawn-send-wait / repo-artifact /
  native-session-poll`; Codex adapter.
- Narrow every packet from the stage allowlist; stage only named files; never
  use all-files staging.
- Preserve genuine RED, focused/integrated/environment gates, exact diff,
  independent review, local commit and ledger evidence per semantic change.
- Stop on the circuit breakers in section 6. Never retry uncertain CAD
  mutation, accept partial discovery, expose arbitrary paths or run two Kernel
  authorities.
- Recovery checks: `git status --short --branch`, `git rev-parse HEAD`, artifact
  revision/authorization readback, active packet allowlist, then the last
  packet's smallest independent gate. Do not launch a duplicate long process;
  poll the original native session.

## 14. Recovery Snapshot P0B-S02

### 1. Completed milestones

- P0B-R1 was independently reviewed `0/0/0`, approved under P0B-A01 and
  committed locally as `6eb209d`.
- P0B-A02 authorizes non-force branch pushes; PR/tag/release remain excluded.

### 2. Next steps

1. Commit this P0B-R1.1 authority amendment.
2. Push `codex/agent-stage3` to `origin` with upstream tracking and verify exact
   local/remote equality.
3. Issue P0B-C01 without repeating P0B-A01 or P0B-A02.

### 3. Approved decisions

- P0B-D01..D22 plus D08A/D17A remain approved under P0B-A01.
- P0B-D22-R1 under P0B-A02 supersedes only the no-push portion of P0B-D22.

### 4. Execution discipline

- Use non-force `git push -u origin codex/agent-stage3` for first publication,
  then verify `HEAD == @{upstream}` after every accepted commit.
- All previous allowlists, gates, circuit breakers and residual boundaries
  remain unchanged. No PR/tag/release/marketplace action is authorized.

## 15. Recovery Snapshot P0B-S03

### 1. Completed milestones

- P0B-R1.1 authority commit `a7e6881` is pushed and exactly equals
  `origin/codex/agent-stage3`.
- C01's unprefixed pytest command was rejected as an environment/setup red;
  corrected explicit-checkout import produced a genuine five-failure RED.

### 2. Next steps

1. Continue C01 only with `PYTHONPATH=src` prepended to pytest commands.
2. Implement replay-safe task creation, run the corrected focused/integrated
   gates and assign a distinct review.
3. Append C01 evidence, commit named files, push and verify remote equality.

### 3. Approved decisions

- P0B-A01/A02 and P0B-D01..D22 plus D08A/D17A/D22-R1 remain active.
- P0B-GATE-CORR-01 corrects only the observed checkout import route under
  P0B-D21; it does not change product behavior or weaken a gate.

### 4. Execution discipline

- Treat the four collection errors as setup evidence only and the five intended
  failures as the sole C01 RED.
- C01's allowlist is expanded within the approved stage to
  `src/vibecad/workflow/store.py` and directly affected public-contract tests;
  no other product scope changes.

## 16. Recovery Snapshot P0B-S04

### 1. Completed milestones

- C01 makes public `create_task` require a retained
  `task_create_[0-9a-f]{32}` key. The domain-separated SHA-256 binds a stable
  task id while the full digest and immutable intent distinguish a truncated
  prefix collision.
- Same-key response loss, restart, current-generation replay, HEAD advance,
  legacy occupants, cross-thread/process races, uncertain durability and
  scheduler oversleep are covered. Legacy schema-v1 records without
  `creation_digest` remain checksum-valid and CAS-compatible; the digest is
  immutable once present.
- Public tool count remains 20. The public schema, idempotence annotation,
  Supervisor replay set, Agent Skill, acceptance guide and runtime receipt
  digest now agree. No FreeCAD/CAD execution path was added.
- Controller gates are green: focused `1025 passed`; affected integration
  `229 passed, 19 deselected`; full non-slow `3902 passed, 95 deselected`.
  Same-key stress passed `200/200` and a final `100/100`; review is `0/0/0`.

### 2. Next steps

1. Commit the exact C01 allowlist as
   `feat(tasks): make task creation replay-safe`.
2. Non-force push `codex/agent-stage3`, then verify exact
   `HEAD == @{upstream}`.
3. Rebind the prepared C02 packet to that accepted remote anchor and begin
   bounded task discovery/events without repeating P0B-A01 or P0B-A02.

### 3. Approved decisions

- P0B-A01/A02 and P0B-D01..D22 plus D08A/D17A/D22-R1 remain active.
- P0B-D21 covers the in-scope concurrency repair and the directly affected
  runtime public-surface receipt anchor; no scope, authority or delivery
  boundary changed.

### 4. Execution discipline

- The retry controller is bounded by a one-second regular window, 512 regular
  attempts and 32 deadline-grace attempts with a 50 ms delay cap. One budget is
  shared through create, `ALREADY_EXISTS` and uncertain-durability readback.
- Preserve P0B-GATE-CORR-01: pytest uses explicit `PYTHONPATH=src`; the hidden
  editable `.pth` collection behavior is environment evidence, not product
  RED.
- PR, tag, release, marketplace publication, force-push and external spend
  remain unauthorized.
