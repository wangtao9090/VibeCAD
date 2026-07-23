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
| P0B-RES-13 | D11 rejects arbitrary client paths, while the existing public `create_project(kind=import_fcstd)` contract requires `source_path`; C07 does not tunnel or resolve this product boundary | C10/C13 entry review; preserve the operation-specific read-only import contract only with explicit evidence, or add a session-bound input/FD grant | all 28 operations route through the daemon without a generic path capability, and import-from-file passes the chosen threat-contract E2E |

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
| P0B-E05 / 2026-07-23T04:57:13Z | P0B-C02 under P0B-R1.1/A01/A02; P0B-D21 direct-impact allowlist repairs | this C02 commit / non-force push required | semantic RED `3 failed` then `5 failed`; focused `1157 passed`; affected integration `224 passed, 8 deselected`; full non-slow `3954 passed, 95 deselected`; 22-tool discovery frame `17,785` bytes; Ruff/format/diff/fsck clean; store and cursor/public reviews both `0/0/0` | Python 3.13 explicit `PYTHONPATH=src` remains; one existing macOS fork deprecation warning; no product residual | P0B-S05 | accepted-green |
| P0B-E06 / 2026-07-23T06:02:10Z | P0B-C03 under P0B-R1.1/A01/A02; P0B-D21 direct-impact and gate-baseline repairs | this C03 commit / non-force push required | API RED `69 failed`; Agent RED `9 failed`; surface RED `14 failed`; core RED plus two review-driven directory-stability/index RED cycles; focused `503 passed`; affected integration `440 passed, 8 deselected`; full non-slow `4074 passed, 95 deselected`; 24-tool frame `18,895` bytes; Ruff/diff/fsck and changed-file format clean; API, public and final core reviews `0/0/0` | explicit `PYTHONPATH=src`; one existing macOS fork warning; full-tree format baseline still names 52 unchanged files, while all 20 changed/new Python files pass; C04 retains CAD payload hashing | P0B-S06 | accepted-green |
| P0B-E07 / 2026-07-23T06:54:17Z | P0B-C04 under P0B-R1.1/A01/A02; P0B-D08/D08A/D21 | this C04 commit / non-force push required | comparison collection RED on missing module and public Agent RED `-32603`; focused `484 passed`; affected integration `614 passed, 8 deselected`; full non-slow `4177 passed, 95 deselected`; 26-tool SDK projection `20,201` bytes / SHA-256 `85914806958d15d0a7d5874566936e098729db406ad2002f9903b834904ca58c`; complete frame `20,246` bytes; public receipt `351d7de2676d6299b0ad906155e47525e59152549e3d82499fa4d05f11aacb5d`; Ruff/diff/fsck and 21-file format clean; storage and final integration reviews `0/0`, API audit `37 passed` | semantic geometry/entity/parameter diff remains explicitly unsupported; no CAD/runtime/materialization on read paths; one existing macOS fork warning; full-tree format baseline remains unrelated | P0B-S07 | accepted-green |
| P0B-E08 / 2026-07-23T07:56:06Z | P0B-C05 under P0B-R1.1/A01/A02; P0B-D10/D21 | this C05 commit / non-force push required | state RED `28 failed`; catalog RED on missing cancel API; two review-driven concurrency/store-race RED cycles; focused `1023 passed`; core with store `1267 passed`; affected public integration `452 passed, 8 deselected`; full non-slow `4385 passed, 95 deselected`; same-intent stress controller `100/100` plus independent `200/200` and `50/50`, all with 16 callers; 27-tool SDK projection `20,717` bytes / SHA-256 `57a38baa2bb79d959037d3066e68468066893b01383cfdd8f77dac447d79e9e8`; complete frame `20,762` bytes / SHA-256 `07e4b2e6be4a3582ffea27b1a194ae6081679448e0f1903b3aaf39b804c86724`; canonical public receipt `627abca4775d57a7a975f385ad95d7ca2d3eb331f2266ffbcf62498456ac2a56`; exact changed-file format/diff/offline-lock/syntax clean; public and state/store/catalog reviews `0/0` | active Worker kill/reconcile remains C12; one existing macOS fork warning; one anchor I001 is excluded only for its unchanged local-import block; full-tree format baseline remains unrelated | P0B-S08 | accepted-green |
| P0B-E09 / 2026-07-23T09:34:39Z | P0B-C06 under P0B-R1.1/A01/A02; P0B-D09/D21 | this C06 commit / non-force push required | semantic RED and review-repair cycles covered key/intent replay, ancestry, seeded-copy binding, accept-time drift and crash recovery; canonical `466 passed`; storage/candidate `576 passed`; affected public/API integration `636 passed`; final audit `944 passed`; full non-slow `4441 passed, 95 deselected`; real FreeCAD 1.1.0 restart/accept smoke `1 passed`; 28-tool SDK projection `21,438` bytes / SHA-256 `5d7703a55dd7b20c21c487d6f4740fbfb894cf6867c840ccb30adf57de63efda`; complete frame `21,483` bytes / SHA-256 `22c903b05fc6e46868bd74380880cca5c915f312ac2ddf24f7e48896b8cdf826`; canonical public receipt `61a9f6c662ad224147aad07b0d701f82a3407d4ec0b8f15ede48dff76c4c98d3`; changed-file Ruff/format/syntax, diff and fsck clean; API, workflow, storage and final diff reviews `Critical 0 / Important 0` | the published Skill projection remains intentionally frozen at the last accepted 27-tool package until C14 refreshes all release artifacts; first two real-smoke attempts selected the wrong managed prefix and are setup evidence, while the declared legacy-compatible prefix passed; one existing macOS fork warning and one unchanged full-Ruff I001 remain | P0B-S09 | accepted-green |
| P0B-E10 / 2026-07-23T10:17:11Z | P0B-C07 under P0B-R1.1/A01/A02; P0B-D11/D12/D13/D21 | this C07 commit / non-force push required | initial semantic RED `19 failed, 24 passed`; independent review RED `4 failed`; final focused `54 passed`; full non-slow `4474 passed, 95 deselected`; real macOS getpeereid POC repeated 32 times in project and FreeCAD Python, plus automated socketpair and bind/listen/connect/accept flows; endpoint path/root replacement and fresh/preinitialized two-process authority races fail closed; changed-file Ruff/format/syntax, diff and fsck clean; final protocol review `Critical 0 / Important 0 / Minor 0`, identity reviews `Critical 0 / Important 0` | runnable daemon, receipt/secret persistence, pre-challenge composition and production EndpointBinding remain C09; Linux/Windows remain P0B-RES-02; existing import `source_path` versus D11 is recorded as P0B-RES-13 for C10/C13; one unchanged macOS fork warning and full-Ruff baseline I001 remain | P0B-S10 | accepted-green |
| P0B-E11 / 2026-07-23T11:33:13Z | P0B-C08 under P0B-R1.1/A01/A02; P0B-D15/D21/D22-R1; D21 packet-allowlist repair adds `src/vibecad/execution/revisions.py` and `tests/test_revision_store.py` for the bounded source-observation seam and its integrity/performance regressions | this C08 commit / non-force push required | semantic RED `14 failed, 36 passed`; performance RED stable live get `9 > 2` actual `_validate_revision_content` passes; review Important REDs covered empty-project first draft `NOT_FOUND` and post-hash valid-manifest replacement returning a stale observation; final focused `66 passed, 1 warning`; affected `458 passed, 1 warning`; full non-slow `4508 passed, 95 deselected, 1 warning`; changed-file Ruff/format, diff and fsck clean; persistence and semantic reviews on exact source/test diff SHA-256 `bdf51474d75f653e57e54989cc7ddb1cad1ba4846ad7ec79744b33657c74dbef` both GO with `Critical 0 / Important 0 / Minor 0` | one unchanged macOS multithreaded-fork deprecation warning; legacy schema-v1 records intentionally recover as `recovery_required`; runnable daemon remains C09 and grants remain C10 | P0B-S11 | accepted-green |
| P0B-E12 / 2026-07-23T12:28:27Z | P0B-C09 under P0B-R1.1/A01/A02; P0B-D02/D11/D12/D18/D21/D22-R1; D21 packet-allowlist repair adds the directly required captured-layout composition seam in `src/vibecad/application/agent.py` plus its tests and continuous authority-liveness validation in `src/vibecad/workflow/lease.py` plus its tests | this C09 commit / non-force push required | daemon semantic RED `5 failed, 17 passed`; composition RED `14 failed, 65 deselected`; authority RED `11 failed, 180 deselected`; review repair cycles close authenticated-socket leakage, early accept-thread start, swallowed fatal handler exceptions and idle-timeout/handler-time ambiguity; final local-daemon `38 passed`; focused C09/affected `352 passed`; full non-slow `4561 passed, 95 deselected, 1 warning`; real macOS auth/double-start/crash-restart/root-and-entry-rebind/8-connection/blocked-shutdown tests pass; controller 50-ping median `3.042 ms`, p95 `3.498 ms`, max `3.782 ms`; full Ruff, changed-file format, diff and fsck clean; two final exact-code reviews both GO with `Critical 0 / Major 0`, 11-file content-manifest SHA-256 `ab5d2fcbb82961946fef0925fe85d209a8561ada957131d4ed9a3f3981eabdc9` | Linux/Windows remain P0B-RES-02 and malicious same-UID replacement remains P0B-RES-03; C10 still owns grants, C11 owns operation-aware Worker deadlines, C13 owns MCP/Workbench routing and P0B-RES-13 import-path resolution; one unchanged macOS multithreaded-fork warning remains | P0B-S12 | accepted-green |
| P0B-E13 / 2026-07-23T13:25:15Z | P0B-C10 under P0B-R1.1/A01/A02; P0B-D14/D15/D21/D22-R1; D21 direct full-gate repair adds `src/vibecad/application/project_create.py` and `tests/test_project_bootstrap.py` to replace volatile ancestor timestamps with stable directory identity while preserving source/path rebinding checks | this C10 commit / non-force push required | protocol RED `9 failed, 16 passed, 43 deselected`; checkout-snapshot RED `12 failed, 66 deselected`; review repair cycles close mint-vs-close grant retention, non-exact open grant descriptors, pre-capture TTL loss and lifetime grant-ID exhaustion; deterministic baseline RED `1 failed, 148 deselected` closes unrelated ancestor-entry churn; canonical C10 `141 passed, 1 warning`; affected `308 passed, 1 warning`; project-bootstrap `149 passed` in three consecutive full-module runs; v1/MCP surface `100 passed, 55 deselected`; final full non-slow `4643 passed, 95 deselected, 1 warning`; full Ruff, exact 13-file format, diff and fsck clean; grant security/contract and baseline security/regression reviews all GO with `Critical 0 / Major 0 / Minor 0`; 13-file implementation/test content-manifest SHA-256 `8ed69c721c064007dab2c49efb236199a4d61ddcd0e63a08525415853e83a6fa` | Linux/Windows remain P0B-RES-02; post-claim malicious same-UID pathname replacement remains P0B-RES-03; import `source_path` remains P0B-RES-13; one unchanged macOS multithreaded-fork warning remains | P0B-S13 | accepted-green |

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

## 17. Recovery Snapshot P0B-S05

### 1. Completed milestones

- C02 adds public `list_tasks` and `get_task_events`; the runtime, manifest,
  Skill and product documentation now agree on an exact 22-tool surface.
- Task discovery holds the catalog lease for one complete, strictly read-only
  fd-relative scan. Every record is validated before pagination, summaries are
  bounded and lightweight, and journal/temp/corrupt/unsafe/capacity failures
  reject the whole request without recovery or partial output.
- Stateless cursors bind endpoint domain, stable store namespace, complete
  validated snapshot or persisted transition history, and absolute offset.
  They survive reopening the same store, reject stale/foreign snapshots, and
  do not bind the requested page size.
- `get_task_events` projects only authoritative `TaskRun.transitions`; no
  timestamped log or second event store was introduced. The two Agent
  facades do not construct a project runtime, CAD port or FreeCAD session.
- The canonical public-surface receipt is
  `a8b31d42abc4ece89d5f6a46a19912520c54ab64d27a5ecc53cb218e10caf5af`.
  The fixed 22-tool discovery frame is 17,785 bytes, below the 32,768-byte
  budget.
- Controller gates are green: focused `1157 passed`; affected integration
  `224 passed, 8 deselected`; full non-slow
  `3954 passed, 95 deselected`. Store and cursor/public independent reviews
  both returned `Critical 0 / Important 0 / Minor 0`.

### 2. Next steps

1. Commit the exact C02 allowlist as
   `feat(tasks): expose bounded task discovery and events`.
2. Non-force push `codex/agent-stage3`, then verify exact
   `HEAD == @{upstream}` and remote ref equality.
3. Rebind C03 to the accepted remote anchor and implement bounded project
   discovery without repeating P0B-A01 or P0B-A02.

### 3. Approved decisions

- P0B-A01/A02 and P0B-D01..D22 plus D08A/D17A/D22-R1 remain active.
- P0B-D21 covers the directly affected runtime receipt, registry uniqueness
  test, server/supervisor projections and current product-truth documents.
  No product scope, authority, delivery target or external action changed.

### 4. Execution discipline

- `list_tasks` is a recovery path for unknown task ids, not a mandatory scan
  after successful creation. Snapshot conflict restarts from page one.
- `get_task_events` is persisted transition audit only. Its cursor conflict
  restarts that task's event pagination from page one.
- Preserve P0B-GATE-CORR-01: pytest uses explicit `PYTHONPATH=src`; the hidden
  editable `.pth` behavior is environment evidence, not product RED.
- PR, tag, release, marketplace publication, force-push and external spend
  remain unauthorized.

## 18. Recovery Snapshot P0B-S06

### 1. Completed milestones

- C03 adds public `list_projects` and `list_revisions`; runtime, manifest,
  Skill and current product documentation now agree on an exact 24-tool
  surface. Project discovery is a recovery path for an unknown project id,
  while revision discovery returns only the canonical-id-sorted complete
  ancestry of the current committed HEAD.
- One global revision-quota lease covers each complete fd-relative read-only
  scan. Project, revisions, candidates, sealed revision, quota, reservations
  and store-root directory identities are pinned across the scan. Unknown
  members, record corruption, unsafe nodes, inconsistent journals,
  missing-base/cyclic lineage and concurrent directory membership changes
  reject the whole request before pagination.
- Normal `STAGING` and old-HEAD `PREPARED` drafts are validated and hidden;
  new-HEAD `PREPARED` and terminal `COMMITTED` states follow committed HEAD;
  terminal `NOT_COMMITTED`, sibling orphan and abandoned revisions remain
  excluded. Candidate payload writes remain outside the committed snapshot.
- Stateless cursors bind endpoint domain, stable store namespace, complete
  validated state, target project where applicable and absolute offset. They
  survive reopening the same store, allow a changed page limit and reject
  stale, cross-store, cross-project or cross-endpoint use.
- Quota ownership uses at most four ancestor-prefix lookups per entry and
  reservation discovery builds one project index, avoiding
  entry-by-reservation and project-by-reservation scans while the global lease
  is held.
- The canonical public-surface receipt is
  `031149f94811b2b99f01ab52dcb8e784c12371082e525fe90189a7b1f6ed5502`.
  The fixed 24-tool SDK projection is 18,850 bytes and its complete JSON-RPC
  frame is 18,895 bytes, below the 32,768-byte budget.
- Controller gates are green: focused `503 passed`; affected integration
  `440 passed, 8 deselected`; full non-slow
  `4074 passed, 95 deselected`. Ruff, exact changed/new-file format, diff and
  fsck checks pass. API, public-surface and final core independent reviews all
  returned `Critical 0 / Important 0 / Minor 0`.

### 2. Next steps

1. Commit the exact C03 allowlist as
   `feat(revisions): expose project and revision discovery`.
2. Non-force push `codex/agent-stage3`, then verify exact
   `HEAD == @{upstream}` and remote-ref equality.
3. Rebind C04 to the accepted remote anchor and implement revision comparison
   plus read-only artifact manifests without repeating P0B-A01 or P0B-A02.

### 3. Approved decisions

- P0B-A01/A02 and P0B-D01..D22 plus D08A/D17A/D22-R1 remain active.
- P0B-D21 covers the directly affected runtime receipt, server/supervisor
  projections, current product-truth documents, directory-stability repair and
  bounded quota indexing. No product scope, authority or delivery boundary
  changed.
- P0B-GATE-CORR-02 records that the broad Ruff format command reports 52
  unchanged baseline files; the exact 20 changed/new Python files pass format
  checking. Those unrelated baseline files are not rewritten into C03.

### 4. Execution discipline

- `list_projects` is used only when the project id is unknown; a cursor
  conflict restarts discovery from page one.
- `list_revisions` is committed-history metadata, not a CAD integrity scan.
  It validates manifests, references, sizes and stable file identity without
  streaming FCStd/STEP bytes. C04 owns tamper and missing-artifact hashing.
- Both list facades avoid `DurableProjectService`, project-write leases, CAD
  runtime construction and FreeCAD imports.
- Preserve P0B-GATE-CORR-01/02 and continue exact `PYTHONPATH=src` plus
  changed-file format gates. PR, tag, release, marketplace publication,
  force-push and external spend remain unauthorized.

## 19. Recovery Snapshot P0B-S07

### 1. Completed milestones

- C04 adds public `compare_revisions` and `get_artifact_manifest`; runtime,
  MCPB manifest, Skill and current product documentation now agree on an exact
  26-tool surface: 20 stable lifecycle/service tools and 6 registry-derived
  direct CAD tools.
- Revision comparison accepts only two revisions in the validated ancestry of
  the same current committed HEAD. It reports direction, base and manifest
  changes plus fixed FCStd/STEP added/removed/modified/unchanged states. It
  hashes the sealed payload bytes and rejects missing or tampered artifacts,
  while geometry/entity/parameter semantic diff is explicitly `unsupported`.
- Artifact manifest observation binds exact task generation,
  committed-or-draft revision, verification report, acceptance id,
  observation digest and the fixed FCStd/STEP pair. A virgin catalog returns
  `materialized=false` without constructing `ArtifactStore`, export service,
  authority, CAD validation port or runtime and without changing the artifact
  tree.
- Only a fully validated existing `PUBLISHED` delivery exposes its two
  canonical resource URIs and delivery-manifest digest. Request digest,
  response binding, directory identity, content hashes, global byte capacity
  and reservation accounting are revalidated read-only; no export,
  materialization, cleanup or repair is triggered by observation.
- The canonical public-surface receipt is
  `351d7de2676d6299b0ad906155e47525e59152549e3d82499fa4d05f11aacb5d`.
  The fixed 26-tool SDK projection is 20,201 bytes with SHA-256
  `85914806958d15d0a7d5874566936e098729db406ad2002f9903b834904ca58c`;
  its complete JSON-RPC frame is 20,246 bytes, below the 32,768-byte budget.
- Controller gates are green: focused `484 passed`; affected integration
  `614 passed, 8 deselected`; full non-slow
  `4177 passed, 95 deselected`. Ruff, changed/new-file format, diff and fsck
  checks pass. Artifact storage and final integration reviews returned
  `Critical 0 / Important 0`; the independent public API audit passed 37
  tests, and a 64-call concurrency probe preserved the lazy no-write boundary.

### 2. Next steps

1. Commit the exact C04 allowlist as
   `feat(artifacts): expose revision comparison and manifests`.
2. Non-force push `codex/agent-stage3`, then verify exact
   `HEAD == @{upstream}` and remote-ref equality.
3. Rebind C05 to the accepted remote anchor and implement durable cancellation
   contracts without repeating P0B-A01 or P0B-A02.

### 3. Approved decisions

- P0B-A01/A02 and P0B-D01..D22 plus D08A/D17A/D22-R1 remain active.
- P0B-D21 covers the directly affected runtime receipt, server/supervisor
  projections, current product-truth documents, strict artifact-manifest API
  and the read-only catalog integrity repairs found in review. No product
  scope, authority or delivery boundary changed.
- `get_artifact_manifest` is observation, not a lightweight alias for export.
  A caller explicitly invokes `export_task_artifacts` only when delivery
  resources are required and the manifest reports `materialized=false`.

### 4. Execution discipline

- Comparison remains committed-history metadata plus sealed payload integrity;
  it must not claim geometric, entity or parameter-level meaning.
- Manifest queries never acquire a CAD runtime or project-write lease and
  never create, clean or repair artifact-catalog entries. Integrity,
  concurrency or capacity ambiguity fails closed.
- Preserve P0B-GATE-CORR-01/02 and continue exact `PYTHONPATH=src` plus
  changed-file format gates. PR, tag, release, marketplace publication,
  force-push and external spend remain unauthorized.

## 20. Recovery Snapshot P0B-S08

### 1. Completed milestones

- C05 adds public `cancel_task` after `resume_task` and before `accept_draft`.
  Runtime discovery, MCPB manifest, Skill and current product documentation now
  agree on an exact 27-tool surface: 21 stable lifecycle/service tools and 6
  registry-derived direct CAD tools.
- `created`, `needs_plan`, `program_ready` and `needs_input` cancel immediately
  through exact-generation task-store CAS. Lost responses, restart and
  concurrent callers replay the same durable `cancelled` result without a
  second transition. Review drafts remain reject-only.
- TaskRun now carries `cancel_requested`, `cancelling` and `cancelled` plus
  request/start/confirm events. The ordinary transition budget remains 128;
  records 129 through 136 are accepted only as cancellation-proven tail. Old
  confirmation events cannot silently resume a cancellation lineage.
- Review-driven concurrency testing closed two real races. Cancel retries the
  same immutable CAS on exact lock contention instead of repeatedly taking
  read leases and starving a writer. The metadata-only presence probe accepts
  only an otherwise safe `nlink=0` observation caused by atomic replacement;
  the lease-held authoritative reader still requires exact `nlink=1` and all
  other owner/mode/device/link/identity checks.
- Idle cancellation does not compose CAD/runtime/artifact services, acquire a
  project write lease, or alter project HEAD, source files or artifact trees.
  MCP `notifications/cancelled` remains transport-only. Active Worker
  termination and cancellation reconciliation remain explicitly deferred to
  C12.
- The canonical public-surface receipt is
  `627abca4775d57a7a975f385ad95d7ca2d3eb331f2266ffbcf62498456ac2a56`.
  The fixed 27-tool SDK projection is 20,717 bytes with SHA-256
  `57a38baa2bb79d959037d3066e68468066893b01383cfdd8f77dac447d79e9e8`;
  its complete JSON-RPC frame is 20,762 bytes with SHA-256
  `07e4b2e6be4a3582ffea27b1a194ae6081679448e0f1903b3aaf39b804c86724`.
- Controller gates are green: focused `1023 passed`; core with store
  `1267 passed`; affected public integration `452 passed, 8 deselected`; full
  non-slow `4385 passed, 95 deselected`. Controller and independent
  concurrency runs covered 350 rounds with 16 callers and produced one durable
  transition per round. Public and state/store/catalog reviews returned
  `Critical 0 / Important 0`.

### 2. Next steps

1. Commit the exact C05 allowlist as
   `feat(tasks): add durable cancellation contracts`.
2. Non-force push `codex/agent-stage3`, then verify exact
   `HEAD == @{upstream}` and remote-ref equality.
3. Rebind C06 to the accepted remote anchor and implement verified forward
   revert without repeating P0B-A01 or P0B-A02.

### 3. Approved decisions

- P0B-A01/A02 and P0B-D01..D22 plus D08A/D17A/D22-R1 remain active.
- P0B-D10 fixes durable cancellation as a TaskRun contract. C05 exposes only
  immediate idle cancellation; active request persistence, Worker kill,
  generation fencing and reconcile are completed together in C12.
- P0B-D21 covers the directly affected task-store presence/CAS repairs,
  runtime receipt, server/supervisor projection, Skill and current
  product-truth documents found during C05 review. No product scope, authority
  or delivery boundary changed.

### 4. Execution discipline

- Retry the same immutable cancellation CAS only for exact
  `LOCK_UNAVAILABLE`. Conflict and uncertain durability use bounded readback;
  unsafe, I/O, capacity and invalid-store failures do not enter mutation
  retry.
- Presence checks are not authoritative record reads. Only the lease-held
  decoder can validate and return TaskRun bytes; symlink, hardlink, persistent
  zero-link, owner/mode/device/root or inode ambiguity remains fail closed.
- `cancel_requested` and `cancelling` are durable future-path contracts, not a
  claim that C05 can stop active FreeCAD. Review uses Reject, and transport
  request cancellation never mutates TaskRun.
- Preserve P0B-GATE-CORR-01/02 and continue exact `PYTHONPATH=src` plus
  changed-file format gates. PR, tag, release, marketplace publication,
  force-push and external spend remain unauthorized.

## 21. Recovery Snapshot P0B-S09

### 1. Completed milestones

- C06 adds public `revert_project` after `compare_revisions` and before
  `create_task`. Runtime discovery, MCPB manifest, server and supervisor now
  expose 28 tools: 22 stable lifecycle/service tools and 6 registry-derived
  direct CAD tools. The packaged Skill remains frozen until C14 performs the
  single release-artifact refresh.
- The exact public intent binds schema version, keyed request, project, source
  revision and expected current HEAD. Same key and intent replays the existing
  TaskRun at its current generation before CAD/runtime construction; changed
  intent conflicts, and a second catalog replay check closes the runtime-gate
  race.
- A new operation accepts only a complete historical ancestor of the exact
  live HEAD. It copies the descriptor-pinned FCStd and sole STEP payload into a
  seeded candidate based on that HEAD, reloads and verifies the copy, then
  enters the ordinary review path. Acceptance creates a new forward revision
  through HEAD CAS; neither the source revision nor the previous HEAD is
  rewritten.
- Seed intent and seed binding are fsync-durable, fail closed on missing or
  altered control records, and are excluded from sealed revisions. Discovery,
  sealing and acceptance revalidate source ancestry, source/candidate payload
  identity and crash-recovery state. Source corruption becomes
  `recovery_required`; candidate tamper remains an ordinary failed task.
- The canonical public-surface receipt is
  `61a9f6c662ad224147aad07b0d701f82a3407d4ec0b8f15ede48dff76c4c98d3`.
  The fixed 28-tool SDK projection is 21,438 bytes with SHA-256
  `5d7703a55dd7b20c21c487d6f4740fbfb894cf6867c840ccb30adf57de63efda`;
  its complete JSON-RPC frame is 21,483 bytes with SHA-256
  `22c903b05fc6e46868bd74380880cca5c915f312ac2ddf24f7e48896b8cdf826`,
  below the 32,768-byte budget.
- Controller gates are green: canonical `466 passed`; storage/candidate
  `576 passed`; affected public/API integration `636 passed`; final independent
  audit `944 passed`; full non-slow `4441 passed, 95 deselected`; and the
  installed FreeCAD 1.1.0 AgentApplication restart/accept smoke `1 passed`.
  Changed-file Ruff, format, syntax, diff and fsck checks pass. Independent
  API, workflow, storage and final diff reviews returned
  `Critical 0 / Important 0`.

### 2. Next steps

1. Commit the exact C06 allowlist as
   `feat(revisions): add verified forward revert`.
2. Non-force push `codex/agent-stage3`, then verify exact
   `HEAD == @{upstream}` and remote-ref equality.
3. Rebind C07 to the accepted remote anchor and prove the macOS peer-euid
   primitive before implementing authenticated local protocol v2.

### 3. Approved decisions

- P0B-A01/A02 and P0B-D01..D22 plus D08A/D17A/D22-R1 remain active.
- P0B-D09 fixes revert as a new, verified, reviewable forward revision. It is
  not HEAD rewind, in-place historical editing, unverified file copy or a
  second execution system.
- P0B-D21 covers the directly affected seeded reservation/storage integrity,
  replay race, strict public result binding, runtime receipt and
  server/supervisor projections found during C06 review. No product scope,
  authority or delivery boundary changed.

### 4. Execution discipline

- Replay the existing immutable TaskRun before live HEAD, CAD or runtime
  checks. A first execution requires the exact expected HEAD and strict
  historical ancestry; acceptance revalidates both source and candidate.
- Preserve exact sealed FCStd/STEP bytes on the copy path. Do not checkpoint,
  export, mutate the source revision, rewind HEAD or treat cleanup as revert.
- The first two real-smoke runs are retained as setup evidence: one used the
  managed prefix inside the same runtime root and one violated the test's
  legacy-prefix contract. The declared legacy-compatible installed FreeCAD
  prefix passed without product-code relaxation.
- Preserve P0B-GATE-CORR-01/02 and continue exact `PYTHONPATH=src` plus
  changed-file format gates. PR, tag, release, marketplace publication,
  force-push and external spend remain unauthorized.

## 22. Recovery Snapshot P0B-S10

### 1. Completed milestones

- C07 preserves the existing non-runnable protocol v1 and adds a separate,
  non-exported `protocol_v2` contract. It is not referenced by the current
  server, supervisor, AgentApplication or Task Kernel, so the accepted 28-tool
  product path and public-surface receipt remain unchanged.
- Protocol v2 freezes a 4-byte big-endian frame header, 1 MiB payload,
  8 connections, 8 in-flight requests, at most 8 decoded frames per feed,
  5-second authentication timeout and 30-second idle timeout. Zero,
  truncated, oversized, aggregate-over-budget and post-finish input all fail
  closed.
- Mutual HMAC-SHA256 authentication binds the boot secret, expected receipt
  daemon id, fresh server/client nonces and a server-generated session. A
  derived connection-local key authenticates every canonical request and
  response; sequence starts at one and is strict, request ids cannot be
  reused, responses may complete out of order only for their exact active
  request object, and any cross-session, tamper or replay path fails before a
  handler.
- `StaticV2Dispatcher` has five code-installed methods only:
  `kernel.ping`, `application.call`, `checkout.open`, `checkout.get` and
  `checkout.close`. Application operations require a bounded immutable
  allowlist supplied by the future daemon facade. No wire `getattr`, callable,
  Python name, environment, internal root or `local_path` can select or reach
  a handler; handler exceptions and invalid results become fixed signed
  errors without raw details.
- The D13 veto is GO on macOS 26.5.2 / Darwin 25.5.0 x86_64. libc
  `getpeereid(2)` returns the exact current euid/egid in system Python 3.14.2,
  project Python 3.13.14 and managed FreeCAD Python 3.12.13. Project and
  FreeCAD environments each passed 32 repeated two-sided observations.
  Missing symbol, native failure, unconnected/closed socket, changed fd and
  different euid reject without a LOCAL_PEERCRED or secret-only fallback.
- Temporary-root POCs require an owned exact-`0700` root and pathname
  `S_ISSOCK`/euid/`0600`/single-link/same-device identity. They prove the
  macOS listener fd inode is not the pathname socket inode, and detect
  endpoint unlink/rebind plus root rename/recreate. A fixed durable authority
  lease produces one process winner; fresh first-entry contention also
  produces exactly one winner and a fail-closed loser, and the authority is
  recoverable after release.
- Controller gates are green: focused `54 passed`; full non-slow
  `4474 passed, 95 deselected`. Changed-file Ruff, format, syntax, diff and
  fsck checks pass. Final independent protocol review returned
  `Critical 0 / Important 0 / Minor 0`; two identity/daemon-boundary reviews
  returned `Critical 0 / Important 0`.

### 2. Next steps

1. Commit the exact C07 allowlist as
   `feat(interaction): define authenticated local protocol v2`.
2. Non-force push `codex/agent-stage3`, then verify exact
   `HEAD == @{upstream}` and remote-ref equality.
3. Rebind C08 to the accepted remote anchor and implement checkout source
   liveness without constructing the runnable daemon early.

### 3. Approved decisions

- P0B-A01/A02 and P0B-D01..D22 plus D08A/D17A/D22-R1 remain active.
- P0B-D11 keeps protocol v1 honest and makes v2 independent. P0B-D12 freezes
  the authentication, permission, identity and budget contract; C09 owns the
  runnable composition. P0B-D13 is satisfied and no revised approval is
  required.
- P0B-D21 covers the four review-driven protocol repairs, post-observation
  socket revalidation and exact POC test expansion. These close existing C07
  boundaries and do not introduce a second execution authority.
- P0B-RES-13 records, without silently resolving, the existing public
  `source_path` contract versus D11's no-arbitrary-path requirement. C07 keeps
  `application.call` operation-specific and closed but does not connect it to
  the application.

### 4. Execution discipline

- C09 must acquire the fixed authority lease against the same captured
  application data/locks identity before constructing AgentApplication. It
  must call `require_same_user_peer` synchronously after accept and before
  constructing or starting `V2ServerConnection`; peer failure can never fall
  back to possession of the boot secret.
- C09 must implement safe run-root creation, production EndpointBinding,
  stale-endpoint handling, canonical `0600` receipt and boot-secret atomic
  persistence, identity-bound cleanup, connection/time limits and
  pre-accept/pre-dispatch root/endpoint revalidation. C07's temporary POC
  helpers are evidence, not a daemon implementation.
- Only the future static daemon facade may supply the immutable application
  operation allowlist. Wire values never drive attribute lookup, import,
  callable selection, environment lookup or internal root selection.
- Preserve P0B-GATE-CORR-01/02 and continue exact `PYTHONPATH=src` plus
  changed-file format gates. PR, tag, release, marketplace publication,
  force-push and external spend remain unauthorized.

## 23. Recovery Snapshot P0B-S11

### 1. Completed milestones

- C08 implements the four P0B-D15 source states: unchanged authority is
  `live`, advanced HEAD is `stale`, changed/accepted/rejected task or draft
  authority is `revoked`, and indeterminate store truth is
  `recovery_required`. Liveness is recomputed on get, keyed replay, restart and
  the acceptance guard; stale, revoked and recovery-required sources cannot
  pass the live/acceptance boundary. Historical checkout bytes remain
  viewable and closable, but the checkout store has no publish authority and
  TaskService remains the sole revision-commit path.
- The existing protocol-v1 projection remains the exact nine-field mapping.
  Local schema v2 adds only source HEAD and source liveness to that projection,
  while durable checkout schema v2 binds the complete `ProjectHead`, including
  generation, and complete `RevisionSourceBinding`. Legacy schema-v1 open and
  tombstone records still decode, but lack sufficient authority evidence and
  therefore fail closed as `recovery_required`.
- Source observation and checkout copy are descriptor-bound and revalidate the
  model, revision directory, revisions directory, configured project root and
  HEAD before/after the relevant operation. Atomic source, directory, root or
  post-hash manifest replacement therefore fails closed. The review-driven
  empty-project repair also proves that a model-less initial HEAD can open its
  first draft, remain live across restart and become stale after HEAD advance.
- The genuine RED sequence was `14 failed, 36 passed`. A separate performance
  RED measured 9 actual `_validate_revision_content` passes for a stable live
  get against the bound of at most 2. Review then exposed two Important REDs:
  empty-project first-draft open returned `NOT_FOUND`, and a valid-checksum
  manifest replacement after payload hashing could return an observation for
  stale metadata. Both defects now have passing regressions.
- The bounded store observation validates a HEAD source once and a distinct
  requested source twice; stable live checkout get performs exactly 2 actual
  `_validate_revision_content` passes. The semantic reviewer independently
  measured a live draft-backed get at 3 passes because that path validates
  current HEAD plus the distinct draft source; this is recorded separately
  from the stable-HEAD bound.
- Final controller gates are green: focused checkout suite
  `66 passed, 1 warning`; affected revision/checkout/discovery suites
  `458 passed, 1 warning`; full non-slow suite
  `4508 passed, 95 deselected, 1 warning`; changed-file Ruff/format, diff and
  fsck checks pass. Persistence and semantic reviewers inspected the exact
  source/test diff SHA-256
  `bdf51474d75f653e57e54989cc7ddb1cad1ba4846ad7ec79744b33657c74dbef`
  and both returned GO with `Critical 0 / Important 0 / Minor 0`.

### 2. Next steps

1. Commit the exact C08 allowlist as
   `feat(interaction): enforce checkout source liveness`.
2. Non-force push `codex/agent-stage3`, then verify exact
   `HEAD == @{upstream}` and remote-ref equality.
3. Rebind C09 to the accepted remote anchor and implement the authenticated,
   single-instance local Task Kernel daemon without pulling C10 file grants
   forward.

### 3. Approved decisions

- P0B-A01/A02 and P0B-D01..D22 plus D08A/D17A/D22-R1 remain active.
- P0B-D15 is satisfied for checkout get/replay/restart and acceptance guards;
  C10 will reuse the same live-source boundary when grant claim is introduced.
- Under P0B-D21, the narrowed C08 packet allowlist added
  `src/vibecad/execution/revisions.py` and
  `tests/test_revision_store.py` solely for the read-only bounded observation
  seam and directly affected integrity/performance tests. Both paths already
  belong to the approved stage allowlist; this changes neither product scope
  nor execution authority.

### 4. Execution discipline

- Preserve protocol-v1's exact nine-field wire mapping, schema-v2's complete
  persisted source binding and fail-closed legacy recovery. Do not expose a
  local path, add checkout publication, construct the daemon or implement
  grants inside C08.
- Use the exact source/test review hash above only for the frozen five-file
  code packet; this append-only documentation update necessarily changes the
  overall working-tree diff hash without changing the reviewed code.
- Continue with `native-plan / spawn-send-wait / repo-artifact /
  native-session-poll`, exact named-file staging, `PYTHONPATH=src` gates,
  immediate non-force push and three-way local/upstream/remote equality.
  PR, tag, release, marketplace publication, force-push and external spend
  remain unauthorized.

## 24. Recovery Snapshot P0B-S12

### 1. Completed milestones

- C09 adds a runnable `python -m vibecad.daemon` production entrypoint and a
  PID-bound local client. One fixed authority lease is acquired against the
  same captured Application data/locks identity before one
  `AgentApplication` is composed. The Application, task/revision stores and
  daemon therefore share one layout and one lease manager rather than opening
  parallel authorities.
- The daemon owns an identity-pinned exact-`0700` run root, `0600` AF_UNIX
  endpoint, random 32-byte boot secret and canonical receipt. Publication is
  secret first and receipt last; cleanup removes the receipt readiness marker
  first. Receipt, secret, endpoint, run root, data root, lock root and authority
  lock are continuously rebound to captured filesystem identities.
  Interrupted publication and verified complete crash leftovers recover;
  unknown, unsafe, live or ambiguously replaced state fails closed.
- Every accepted socket passes the real macOS same-user check before protocol
  state or a challenge is constructed. Mutual protocol-v2 authentication,
  strict signed sequencing and the static five-method dispatcher remain the
  only wire path. The daemon facade contains 24 literal Application operations
  and no reflection/import/callable lookup. The existing public surface remains
  exactly 28 tools.
- `kernel.ping`, path-free checkout open/get/close and the Application request
  facades run through a real authenticated client. C09 explicitly rejects both
  `create_project(kind=import_fcstd)` and any non-null `source_path` before the
  Application; it does not pretend to have resolved P0B-RES-13 and does not
  introduce a C10 file grant early.
- Admission is capped at eight simultaneous connections. Handshake and
  between-request idle deadlines are absolute transport bounds. Handler
  execution does not consume the response-send budget or a client-side idle
  budget; once the first response fragment arrives, fragment-idle protection
  applies. C11 remains responsible for operation-aware, killable Worker RPC
  deadlines.
- Pre-accept, pre-authentication, pre-dispatch and post-dispatch checks bind the
  live authority and published state. A run-root, endpoint, receipt, secret or
  authority-lock replacement stops the service before another dispatch.
  Unknown handler `BaseException` is terminal. EOF, bad proof and ordinary
  protocol failure terminate only that connection. Shutdown drains accepted
  workers before closing the Application and removes receipt/secret/endpoint
  before releasing authority; a blocked worker or unsafe cleanup retains the
  authority and returns `recovery_required`.
- The genuine RED sequence was daemon `5 failed, 17 passed`, captured-layout
  composition `14 failed, 65 deselected`, and authority liveness
  `11 failed, 180 deselected`. Review exposed four Major regressions:
  authenticated socket leakage after the final boot-state check, accept-thread
  start before final validation, silent continuation after fatal handler
  exceptions, and misuse of transport idle as a handler total deadline. Each
  now has a passing regression.
- Final controller gates are green: local daemon `38 passed`; combined
  protocol/daemon/Application/lease `352 passed`; full non-slow
  `4561 passed, 95 deselected, 1 warning`. Fifty warm authenticated pings
  measured median `3.042 ms`, p95 `3.498 ms` and maximum `3.782 ms`. Full Ruff,
  exact 11-file format, diff and fsck gates pass. Two independent final reviews
  returned GO with `Critical 0 / Major 0`; the frozen implementation/test
  content-manifest SHA-256 is
  `ab5d2fcbb82961946fef0925fe85d209a8561ada957131d4ed9a3f3981eabdc9`.

### 2. Next steps

1. Commit the exact C09 allowlist as
   `feat(daemon): add the single-instance kernel service`.
2. Non-force push `codex/agent-stage3`, then verify exact
   `HEAD == @{upstream}` and remote-ref equality.
3. Rebind C10 to the accepted remote anchor and add session-bound file grants
   without changing protocol v1, the 28 public tools or Task Kernel ownership.

### 3. Approved decisions

- P0B-A01/A02 and P0B-D01..D22 plus D08A/D17A/D22-R1 remain active.
- C09 satisfies the runnable macOS single-Kernel portion of P0B-D02/D11/D12
  and the daemon half of P0B-D18. It does not claim C13 consumer routing, a
  Workbench UI, Linux/Windows support or protection against a malicious
  same-UID process.
- Under P0B-D21, the narrowed C09 packet allowlist adds
  `src/vibecad/application/agent.py`, `tests/test_agent_application.py`,
  `src/vibecad/workflow/lease.py` and `tests/test_workflow_lease.py`. These
  files provide the directly required same-layout/same-manager composition
  seam and continuous authority-liveness proof; they add no second execution
  system, modeling operation, grant or public tool.

### 4. Execution discipline

- Preserve the frozen protocol-v1 and protocol-v2 wire contracts. C10 may add
  only the approved session-bound grant method and managed-file broker; it
  must re-evaluate C08 source liveness at claim and may not accept a
  client-supplied path.
- Use the exact review hash above only for the frozen 11 implementation/test
  files. This append-only ledger/snapshot edit necessarily changes the overall
  commit diff without changing the reviewed code.
- Continue with `native-plan / spawn-send-wait / repo-artifact /
  native-session-poll`, exact named-file staging, `PYTHONPATH=src` gates,
  immediate non-force push and three-way local/upstream/remote equality.
  PR, tag, release, marketplace publication, force-push and external spend
  remain unauthorized.

## 25. Recovery Snapshot P0B-S13

### 1. Completed milestones

- C10 adds exactly one protocol-v2 method, `file_grant.claim`, so the static
  dispatcher now has six methods. `checkout.open` remains path-free and
  returns one exact four-field grant descriptor. Only an exact successful
  claim response may contain `local_path`; server encoding and client decoding
  both validate the method-specific grant, checkout, purpose, digest, size and
  canonical managed-file path contracts.
- Each authenticated `V2ServerConnection` supplies its server-generated
  session ID to a per-connection dispatcher. No session or path comes from
  request parameters. The in-memory broker binds every grant to daemon,
  session, checkout, purpose and an identity-bound live-file snapshot.
  Guessing, replay and cross-session claim return the same unavailable result,
  while a cross-session attempt cannot consume the owner's grant.
- Grants are one-shot, use a real 30-second post-capture monotonic lifetime,
  and are capped at eight active grants per session and 64 per daemon. A new
  open for the same session/checkout/purpose replaces the prior unclaimed
  grant. A bounded 65,536-ID recent window prevents practical replay without
  imposing a lifetime mint limit, and active IDs remain protected even after
  recent-window eviction.
- Grant mint and claim both observe a frozen `CheckoutFileSnapshot` containing
  root, checkout-directory and model-file identities plus digest, size, path
  and C08 source liveness. Claim recomputes all of them. Closed, stale,
  revoked, recovery-required, symlinked, hardlinked, replaced, modified or
  otherwise rebound checkouts fail closed before a path is returned.
- Checkout close performs a pre-close and post-close revocation barrier.
  Session EOF clears that session, daemon shutdown clears the broker before
  closing the Application, and restart begins with an empty daemon-bound
  registry. Deterministic close-vs-mint and claim-vs-revoke barriers prove no
  grant survives a completed close or wins a revoked claim.
- The real production composition is exercised without a fake Application:
  `LocalKernelClient -> protocol v2 -> LocalKernelDaemon -> LocalKernelFacade
  -> AgentApplication -> ManagedCheckoutStore`. The open response is
  path-free, claim returns the real private `0600` FCStd with matching bytes,
  and checkout close removes the managed working copy.
- Four review-driven Major defects were closed: a grant minted during checkout
  close could remain active, open grant descriptors lacked method-specific
  exact validation, slow snapshot capture shortened the advertised TTL, and a
  lifetime 65,536-ID set could permanently exhaust a long-running daemon.
  Independent grant security and contract reviews now return GO with
  `Critical 0 / Major 0 / Minor 0`.
- Full-gate repetition also exposed an older source-import false rejection:
  mutable ancestor-directory `mtime/ctime` had been treated as directory
  identity, so unrelated temporary-directory activity could return
  `invalid_input`. The D21 repair binds `dev/inode/mode/uid/gid` instead while
  retaining descriptor-relative `O_NOFOLLOW` traversal, entry-to-FD rebinding
  checks and the source file's complete identity. Its deterministic RED is
  green, the complete project-bootstrap module passed three consecutive
  149-test runs, and two independent reviews report
  `Critical 0 / Major 0 / Minor 0`.
- Final controller gates are green: canonical C10
  `141 passed, 1 warning`; affected C10
  `308 passed, 1 warning`; v1/MCP surface
  `100 passed, 55 deselected`; full non-slow
  `4643 passed, 95 deselected, 1 warning`. Full Ruff, exact 13-file format,
  diff and fsck gates pass. The frozen implementation/test content-manifest
  SHA-256 is
  `8ed69c721c064007dab2c49efb236199a4d61ddcd0e63a08525415853e83a6fa`.

### 2. Next steps

1. Commit the exact C10 allowlist as
   `feat(interaction): add session-bound file grants`.
2. Non-force push `codex/agent-stage3`, then verify exact
   `HEAD == @{upstream}` and remote-ref equality.
3. Rebind C11 to the accepted remote anchor and isolate FreeCAD in one
   killable Worker generation without giving the Worker Task/Revision
   authority.

### 3. Approved decisions

- P0B-A01/A02 and P0B-D01..D22 plus D08A/D17A/D22-R1 remain active.
- C10 satisfies P0B-D14 and the grant-claim portion of P0B-D15. TaskService
  remains the only revision-commit authority; grants expose only daemon-created
  managed checkout files and do not create a second execution system.
- Protocol v1, the MCP transport and the public 28-tool surface are unchanged.
  C10 does not solve arbitrary input import; P0B-RES-13 remains explicitly
  owned by the C13 entry review.
- Under P0B-D21, the narrowed C10 packet adds
  `src/vibecad/application/project_create.py` and
  `tests/test_project_bootstrap.py` solely to close the reproducible full-gate
  ancestor-timestamp false rejection. Stable identity and attack tests prove
  that this repair does not weaken source or namespace rebinding checks.

### 4. Execution discipline

- C11 may use only parent-reserved candidate staging through the frozen Worker
  protocol. It must not reinterpret C10 grants as arbitrary Worker filesystem
  capabilities, add Task/Revision authority to the child, or expose a second
  public execution route.
- Use the exact review hash above only for the frozen 13 implementation/test
  files. This append-only ledger/snapshot edit necessarily changes the overall
  commit diff without changing the reviewed code.
- Continue with `native-plan / spawn-send-wait / repo-artifact /
  native-session-poll`, exact named-file staging, `PYTHONPATH=src` gates,
  immediate non-force push and three-way local/upstream/remote equality.
  PR, tag, release, marketplace publication, force-push and external spend
  remain unauthorized.
