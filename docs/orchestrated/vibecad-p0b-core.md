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
| P0B-RES-14 | C12's gate-free durable revert preparation reuses the private `_load_revert_source_from_store` helper from `workflow.service`; exact lease, binding and atomic creation-disposition tests cover current behavior, but ownership is split across application and service layers | non-blocking internal-structure follow-up; keep the single Task/Revision authority and do not duplicate execution | one formal store-only TaskService preparation contract owns source validation plus create disposition, and the existing replay/cancel/lost-response matrix passes unchanged |

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
| P0B-E14 / 2026-07-23T17:02:31Z | P0B-C11 under P0B-R1.1/A01/A02; P0B-D16/D17/D17A/D21/D22-R1 | this C11 commit / non-force push required | canonical C11 `341 passed, 1 deselected`; Worker non-slow `54 passed, 1 deselected` in three consecutive runs; real managed FreeCAD 1.1.0 `1 passed, 54 deselected`; adapter/program/static `163 passed, 1 deselected`; controller full `4698 passed, 96 deselected, 17 warnings`, independently repeated with the same counts; changed-file compile, Ruff, format and diff clean; three independent final reviews all GO with `Critical 0 / Important 0`; no residual Worker process or private Worker directory; 11-file implementation/test content-manifest SHA-256 `80006968e1611d048be5bda7f1a36c758d68a41224ad53545ac6eff729556de7` | C11 is an injectable Worker substrate only; the default `AgentApplication` path remains in-process until C12 owns injection plus durable loss/cancel/reconcile; the existing macOS Python 3.13 multithreaded-fork warning class remains outside C11 | P0B-S14 | accepted-green |
| P0B-E15 / 2026-07-23T22:01:18Z | P0B-C12 under P0B-R1.1/A01/A02; P0B-D01/D05/D09/D10/D16/D17/D17A/D18/D21/D22-R1; D21 direct-impact repair adds `src/vibecad/runtime/spec.py` because `request_active_cancel` changes the canonical public contract digest while installed-package refresh remains C14 | this C12 commit / non-force push required | semantic and review RED cycles close unproved cancellation laundering, same-generation termination retry, self-loss eviction, concurrent cancel convergence, orphan reservation cleanup, pre-CAD revert durability and raced-existing create disposition; managed M03 final `10 passed, 80 deselected` across load/mutation/checkpoint/STEP/evidence × hang/crash; active-cancel fixture final `10/10`; Worker non-slow `79 passed, 11 deselected`; canonical C12 `1016 passed, 22 deselected`; controller full `4830 passed, 107 deselected, 17 warnings`, independently repeated with the same counts; 28-tool SDK projection remains `21,438` bytes and complete frame `21,483` bytes; canonical public receipt SHA-256 `ae495ba457af40a5837a03e77eef4b396b0a4209755878350bc341ac7de8bfd3`; full Ruff, exact 30-file format/compile, diff and fsck clean; independent final review GO `Critical 0 / Important 0 / Medium 1`; complete staged source/tests diff SHA-256 `47a6b13a0be9b98aa1e1081ac4dd5b49262f504dbd10269907c728d804d8b7fd`; 30-file content-manifest SHA-256 `239c37de120971fe36c98ca7f371d07b159ecf1a594cff09257f7d4a9b7a4e2c` | P0B-RES-06 permits only TaskRun-referenced sealed non-HEAD evidence revisions until GC; P0B-RES-14 records the non-blocking private preload-helper ownership split; 54 unchanged files remain outside the changed-file format gate; the existing 17 macOS multithreaded-fork warnings remain | P0B-S15 | accepted-green |
| P0B-E16 / 2026-07-24T00:47:05Z | P0B-C13 under P0B-R1.1/A01/A02; P0B-D02/D11/D14/D18/D21/D22-R1; P0B-C13-D21-01/D21-02 | this C13 commit / immediate non-force push required | review-driven RED/GREEN cycles close descriptor/path tunnelling, shared-OFD offset races, response rewriting, long admitted-drain truncation, uninstall/startup ABA, incomplete retirement proof and public Workbench marker bypass; canonical C13 `386 passed, 1 warning`; direct impact `526 passed, 1 deselected`; real MCP-created M04 Accept/Reject `2 passed` in two consecutive runs; real managed FreeCAD Worker smoke `1 passed, 89 deselected` in two consecutive runs; final full non-slow `4890 passed, 107 deselected, 19 warnings` after one unrelated active-cancel observation flake passed `4/4` focused; 28-tool SDK projection remains `21,438` bytes and complete frame `21,483` bytes; full changed-file Ruff/format/compile, diff and fsck clean; independent semantic/exact-diff and protocol/FD/lifecycle reviews both GO `Critical 0 / Major 0 / Medium 0`; 21-file source/test content-manifest SHA-256 `16965a7573434a491ad0bad7d884a88b22e19c64ea3223c53d099b84eaf40eaa` | C14 still owns installed-package and exact managed-receipt refresh before the current-runtime public FreeCAD matrix; the legacy slow isolation test still asserts pre-C12 per-project executor object identity; 19 warnings are the expected malformed-ancillary warning plus existing macOS multithreaded-fork deprecations; the separate research document remains excluded | P0B-S16 | accepted-green |
| P0B-E17 / 2026-07-24T03:41:36Z | P0B-C14 under P0B-R1.1/A01/A02; P0B-D02/D11/D14/D16/D17/D17A/D18/D21/D22-R1; P0B-C14-D21-01..22 | this C14 commit / immediate non-force push required | version/package/Skill/docs advance together to 0.6.0 while epoch 4, FreeCAD 1.1.0, MCP 1.27.2, the exact 28-tool contract and public digest `ae495ba457af40a5837a03e77eef4b396b0a4209755878350bc341ac7de8bfd3` remain fixed; final wheel/sdist/MCPB/Skill SHA-256 values are `3c73451a...` / `4fc514cd...` / `1eb2f468...` / `db27e094...`; exact-wheel M05 preserves prefix device/inode, 12,426-entry immutable engine identity, complete live engine manifest and user data while committing the canonical 274-byte receipt `b154e218...`; final full non-slow `4902 passed, 108 deselected, 19 warnings`; slow Worker/P0-B `11 passed, 102 deselected`; current managed candidate/public matrix `2 passed`; fresh MCPB acceptance `1 passed`; M05 `1 passed`; full Ruff, 14-file format/compile, offline lock, version, YAML, diff and fsck gates clean; package/release, runtime-preservation and semantic/docs reviews all GO, final independent signature `Critical 0 / Major 0 / Medium 0 / Minor 0` | no automatic mid-refresh crash resume; exact installed-package proof covers all Python sources plus wheel provenance; the default live M05 data root was absent while non-empty data preservation is covered separately; G1 Qt Workbench, reverse reconstruction, photo-to-mesh and simulation remain later product stages; the separate research document remains excluded | P0B-S17 | accepted-green |

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

## 26. Recovery Snapshot P0B-S14

### 1. Completed milestones

- C11 adds an injectable FreeCAD Worker substrate without creating another
  public execution route. The existing executor keeps its public all-at-once
  seam while a private incremental cursor lets the Worker execute validated
  operations and report bounded progress.
- A strict canonical-JSON RPC codec, opaque generation capabilities and
  descriptor-bound candidate staging keep Task, Revision and project-path
  authority in the parent. The child receives only the exact operation and
  candidate-file capabilities required for one generation.
- macOS Worker launch uses direct `posix_spawn` with a new process group,
  close-on-exec defaults and a minimal private environment. Exact per-operation
  deadlines terminate the entire generation rather than leaving an
  unobservable FreeCAD descendant.
- Cleanup responsibility exists before spawn and remains recoverable through
  connection-close, signal, reap, process-group observation and private-home
  failures. A generation reaches `DEAD` only after child identity is released,
  the process group is absent, the connection is closed and the private home
  is verified absent.
- Candidate authority is revalidated throughout execution. Artifact size,
  digest, identity and claim invariants fail closed across replacement,
  mutation and cross-capability attempts.
- Real managed FreeCAD 1.1.0 completed create/modify/checkpoint/export/reload
  smoke coverage through the Worker. Final controller gates are canonical C11
  `341 passed, 1 deselected`; Worker non-slow
  `54 passed, 1 deselected` in three consecutive runs; adapter/program/static
  `163 passed, 1 deselected`; real FreeCAD
  `1 passed, 54 deselected`; and full
  `4698 passed, 96 deselected, 17 warnings`. An independent full-suite repeat
  produced the same counts.
- Three independent exact-code reviews return GO with
  `Critical 0 / Important 0`. Compile, changed-file Ruff and format, diff and
  residual-process gates pass. The frozen 11-file implementation/test
  content-manifest SHA-256 is
  `80006968e1611d048be5bda7f1a36c758d68a41224ad53545ac6eff729556de7`.

### 2. Next steps

1. Commit the exact C11 allowlist as
   `feat(worker): isolate FreeCAD in a killable generation`.
2. Non-force push `codex/agent-stage3`, then verify exact
   `HEAD == @{upstream}` and remote-ref equality.
3. In C12, inject the Worker-backed `CadExecutionPort` into the default
   application path and make Worker loss, timeout, cancellation and startup
   reconciliation durable through Task Kernel state.

### 3. Approved decisions

- P0B-A01/A02 and P0B-D01..D22 plus D08A/D17A/D22-R1 remain active.
- C11 is the process-isolation substrate, not a second execution system.
  TaskService and RevisionStore remain the only commit authorities, and no
  public tool or alternate client route bypasses them.
- The same-UID Worker is a trusted child constrained by narrow capabilities;
  C11 does not claim a hostile-process sandbox.
- Default `AgentApplication` execution intentionally remains in-process at
  this snapshot. C12 owns the one default-injection switch and its durable
  recovery semantics, so C11 must not be presented as the final user path.

### 4. Execution discipline

- C12 may switch only the approved `CadExecutionPort` composition and add
  durable Worker loss/cancel/reconcile handling through existing Task Kernel
  state. It may not introduce another scheduler, revision authority, public
  tool family or direct client-to-Worker path.
- Use the exact review hash above only for the frozen 11 implementation/test
  files. This append-only ledger/snapshot edit necessarily changes the overall
  commit diff without changing the reviewed code.
- Continue with `native-plan / spawn-send-wait / repo-artifact /
  native-session-poll`, exact named-file staging, `PYTHONPATH=src` gates,
  immediate non-force push and three-way local/upstream/remote equality.
  PR, tag, release, marketplace publication, force-push and external spend
  remain unauthorized.

## 27. Recovery Snapshot P0B-S15

### 1. Completed milestones

- C12 switches the default Application CAD path to one application-owned
  `WorkerCadExecutionPort`. A lost or cancelled generation clears every cached
  project runtime only after exact termination proof; an uncertain cleanup
  retains the same Worker handle and generation poison so the same durable
  request can retry without laundering uncertainty or starting another CAD
  generation.
- Active cancellation is now one Task Kernel lineage. Admission-bound
  execution, idle tasks with exact orphan candidate reservations, concurrent
  callers, response loss and restart all converge through durable
  `REQUEST_CANCEL` or `REQUEST_ACTIVE_CANCEL`, `START_CANCELLATION` and
  `CONFIRM_CANCELLED` events. An unstarted durable request remains requested;
  store-only reconciliation never invents termination proof.
- RevisionStore exposes a read-only, fail-closed reservation-presence probe and
  task-scoped cleanup. Ordinary idle cancellation remains one store CAS with
  no project lease or CAD start, while an exact orphan reservation acquires
  the project lease, starts cancellation and removes only the bound candidate.
- A first revert request validates its immutable source and persists the bound
  `PROGRAM_READY` task before entering the CAD gate. Catalog creation returns
  an atomic disposition: only a directly proven create may start CAD;
  pre-existing tasks, `ALREADY_EXISTS` and durability-uncertain readback are
  replay-only. Queued and runtime-load-blocked reverts are therefore visible,
  cancellable and restart-safe without touching HEAD or source bytes.
- P0B-M03 executes literal managed FreeCAD 1.1.0 load, mutation, checkpoint,
  STEP export and post-export evidence windows under both hang and crash.
  Final controller evidence is `10 passed, 80 deselected in 115.78s`; the
  independent frozen run is `10 passed, 80 deselected in 129.18s`. Every case
  keeps the same daemon alive, removes the old PID/process group/private home,
  preserves HEAD and immutable source data, accepts a fresh client and proves
  a different healthy generation with reloadable FCStd/STEP and exactly one
  intended HEAD advance. The two post-seal evidence cases retain exactly one
  immutable non-HEAD revision referenced by the durable TaskRun; all other
  cases leave the revision set unchanged and no case leaves a live candidate,
  reservation, temporary file or unreferenced revision.
- Final controller gates are Worker non-slow
  `79 passed, 11 deselected in 15.31s`, canonical C12
  `1016 passed, 22 deselected in 44.49s`, and full non-slow
  `4830 passed, 107 deselected, 17 warnings in 244.34s`. The independent full
  run produced the same counts in `232.57s`. A flaky fake-Worker publication
  fixture was closed with exact PID-content polling, the approved 15-second
  cold-readiness bound and strict same-generation recovery retry; its frozen
  stress gate is `10/10`.
- The 28-tool discovery projection remains `21,438` bytes with SHA-256
  `5d7703a55dd7b20c21c487d6f4740fbfb894cf6867c840ccb30adf57de63efda`;
  the complete frame remains `21,483` bytes with SHA-256
  `22c903b05fc6e46868bd74380880cca5c915f312ac2ddf24f7e48896b8cdf826`.
  The output-contract change updates the canonical public receipt to
  `ae495ba457af40a5837a03e77eef4b396b0a4209755878350bc341ac7de8bfd3`;
  C14 still owns installed-package and managed-receipt refresh.
- Full Ruff, compile, exact 30-file format, diff and fsck gates pass. The
  unchanged full-format baseline is `54 files would be reformatted,
  125 already formatted`; none of those 54 files is in C12. The frozen
  complete staged source/tests diff SHA-256 is
  `47a6b13a0be9b98aa1e1081ac4dd5b49262f504dbd10269907c728d804d8b7fd`;
  the 30-file content-manifest SHA-256 is
  `239c37de120971fe36c98ca7f371d07b159ecf1a594cff09257f7d4a9b7a4e2c`.
  Independent final review is GO with
  `Critical 0 / Important 0 / Medium 1`; the Medium is P0B-RES-14.

### 2. Next steps

1. Stage the exact 30 frozen source/test files plus this artifact, excluding
   untracked `docs/CAD_AGENT_PRODUCT_RESEARCH.md`, and commit as
   `feat(kernel): reconcile worker loss and active cancellation`.
2. Non-force push `codex/agent-stage3`, then verify exact
   `HEAD == @{upstream} == origin/codex/agent-stage3`.
3. Recover C13 from that remote anchor and route MCP plus the fake Workbench
   client through the one daemon-owned Task Kernel without changing the
   28-tool public modeling surface.

### 3. Approved decisions

- P0B-A01/A02 and P0B-D01..D22 plus D08A/D17A/D22-R1 remain active.
- Worker generation loss, timeout and cancellation never authorize a second
  scheduler, revision authority or direct client-to-Worker route. TaskService
  and RevisionStore remain the only transition and commit authorities.
- D21 directly adds `src/vibecad/runtime/spec.py` to C12 because the new
  public cancellation event changes the canonical output-contract digest.
  This does not perform or claim the C14 installed package/runtime refresh.
- P0B-RES-06 continues to own mark/quarantine/sweep GC. A sealed revision
  explicitly referenced by a durable TaskRun is auditable state, not an
  orphan. P0B-RES-14 records the non-blocking application/service helper
  ownership split and its exact closure condition.

### 4. Execution discipline

- Preserve the frozen source/tests hashes above while appending this ledger
  evidence. Stage only the named C12 files and this artifact; do not include
  the separate product-research document.
- Continue with `native-plan / spawn-send-wait / repo-artifact /
  native-session-poll`, exact named-file staging, immediate non-force push and
  three-way local/upstream/remote equality.
- C13 may add only thin MCP/Workbench clients over the one daemon. PR, tag,
  release, marketplace publication, force-push and external spend remain
  unauthorized.

## 28. Task Packet P0B-C13

### 1. Entry state and product outcome

- The accepted and remotely verified anchor is
  `a5fba84b8c50980f73051de563445c1138fad7b3`; local `HEAD`, upstream and
  `origin/codex/agent-stage3` are equal.
- C09-C12 provide an authenticated daemon, session-bound managed-file grants,
  a killable FreeCAD Worker and durable cancellation/reconciliation. The MCP
  server still constructs an in-process `AgentApplication`, so those
  facilities are not yet the product's sole execution path.
- C13 makes every stateful project/revision/task/review/artifact/modeling
  operation use one authenticated daemon-owned `AgentApplication` and Task
  Kernel. Runtime installation/status controls remain bootstrap-local and
  inert discovery remains daemon-independent.
- After C13, an MCP client and a public fake-Workbench client can reconnect,
  observe the same task generation, draft/revision hashes and verdict, claim
  the same daemon-created checkout safely, and converge on one durable
  Accept/Reject and HEAD result. Client EOF never stops the daemon. This is the
  G1 backend contract, not a FreeCAD Qt Workbench or visual-diff claim.
- The unchanged pre-RED C13 baseline is
  `289 passed in 12.46s` for
  `tests/test_interaction_protocol.py`, `tests/test_local_daemon.py`,
  `tests/test_mcp_transport.py`, `tests/test_server_agent_surface.py` and
  `tests/test_server_new_tools.py`.

### 2. Bound design decisions

- **P0B-C13-D01 — One stateful route.** The MCP adapter and the public local
  client are thin facades over `LocalKernelClient`; neither may construct
  `AgentApplication`, a Task/Revision store or a CAD Worker. The four runtime
  maintenance controls and bootstrap-safe static capabilities remain local so
  daemon startup never depends on runtime installation.
- **P0B-C13-D02 — Bounded daemon bootstrap.** A managed-runtime client first
  authenticates to the published daemon. If none is usable, it may spawn the
  fixed `python -B -m vibecad.daemon` entry with closed descriptors and a new
  process group, then converge within a fixed deadline. Concurrent starters
  rely on the existing daemon authority lease, and losing starters exit.
  Closing or losing a client closes only its authenticated session; daemon
  shutdown is never coupled to MCP/Workbench EOF.
- **P0B-C13-D03 — No transport retry of uncertain writes.** The high-level
  application adapter preserves the existing envelopes and exact operation
  allowlist. It never transparently retries a call after send/response
  uncertainty. Recovery uses the already durable operation key, list/get or
  explicit resume path against a new authenticated client.
- **P0B-C13-D04 — Descriptor-bound import closes P0B-RES-13.** The public MCP
  `create_project(kind=import_fcstd, source_path=...)` schema remains stable,
  but the MCP process opens the exact same-user, single-link, bounded regular
  file locally and transfers only that descriptor through a session-bound
  macOS `SCM_RIGHTS` capability. No path crosses protocol v2. A
  domain-separated locator digest plus exact file identity binds replay;
  symlink, hardlink, FIFO/directory, unexpected or truncated ancillary data,
  descriptor count, replacement and mutation races fail closed. The daemon
  copies from the received descriptor into its existing private project-create
  transaction and never reopens a client path.
- **P0B-C13-D05 — Immutable resources stay readable without a second writer.**
  MCP resource reads use a strict read-only artifact reader over already
  published immutable materializations. It opens only existing private
  directories and records, performs no mkdir/create/lock/recovery/cleanup or
  FreeCAD work, and revalidates record, directory, file, size and hash
  bindings. This preserves the existing 64 MiB resource contract without
  tunnelling base64 through protocol v2's 1 MiB frame and without constructing
  a second `ArtifactStore` writer.
- **P0B-C13-D06 — Surface stability.** Public names, schemas, annotations,
  result envelopes, ResourceLinks and discovery bytes remain unchanged at 28
  tools. The fake Workbench imports only the public local client package and
  receives no internal root, secret, TaskService, RevisionStore or Worker
  object.
- **P0B-C13-D07 — Crash-safe Kernel retirement.** Ordinary client close and
  EOF remain session-only. Confirmed runtime removal uses the authenticated,
  daemon-id-bound internal `kernel.retire` method; its acknowledgement is sent
  before admission closes, already-admitted responses drain before idle
  sessions/Application/Worker close, and the caller waits for the exact
  receipt/secret/socket/authority publication to disappear. Ping binds the API
  epoch, package version and Kernel build identity, so an old daemon fails
  before any Application call; the managed opener may authenticated-retire that
  exact incompatible daemon and perform one bounded bootstrap retry, but never
  retries an Application mutation. A pending uninstall marker blocks daemon
  connect/start, and pending removal must retire the Kernel before deleting a
  runtime; failure preserves both marker and runtime.

These choices implement P0B-D02/D11/D14/D18 and select the descriptor-capability
branch already reserved by P0B-RES-13. They do not widen the product boundary
or require another user decision under P0B-D21.

### 3. Exact implementation allowlist

Only the following files may change in C13. A file remains untouched when its
RED does not require it:

- `src/vibecad/server.py`
- `src/vibecad/daemon/__init__.py`
- `src/vibecad/daemon/client.py`
- `src/vibecad/daemon/facade.py`
- `src/vibecad/daemon/service.py`
- new `src/vibecad/daemon/bootstrap.py`
- new `src/vibecad/daemon/adapters.py`
- `src/vibecad/interaction/protocol_v2.py`
- `src/vibecad/application/agent.py`
- `src/vibecad/application/project_create.py`
- `src/vibecad/application/artifacts.py`
- `src/vibecad/runtime/status.py`
- `src/vibecad/runtime/uninstall.py`
- `tests/test_interaction_protocol.py`
- `tests/test_local_daemon.py`
- `tests/test_server_agent_surface.py`
- `tests/test_server_new_tools.py`
- `tests/test_agent_application.py`
- `tests/test_project_bootstrap.py`
- `tests/test_artifact_materialization.py`
- `tests/test_uninstall.py`
- new `tests/test_p0b_acceptance.py`
- this orchestration artifact

`src/vibecad/mcp_transport.py`, `src/vibecad/supervisor.py`, public tool specs,
package/version/release metadata and documentation are read-only unless a
genuine C13 RED proves a direct defect and P0B-D21 is recorded before the edit.
The untracked `docs/CAD_AGENT_PRODUCT_RESEARCH.md` is outside the P0-B
allowlist and commit budget and remains excluded.

**P0B-C13-D21-01 — exact allowlist repair.** The confirmed-uninstall RED proved
that a server can durably write `.uninstall_requested`, crash before retiring
the detached daemon, and then let the next supervisor delete the interpreter
under that live daemon. The original C13 allowlist could not close this crash
window. Under P0B-D21, only `src/vibecad/runtime/uninstall.py` and its existing
`tests/test_uninstall.py` are added above. This is a correctness repair inside
the already-approved runtime lifecycle boundary; it adds no public tool,
product capability or user decision.

**P0B-C13-D21-02 — startup-authority allowlist repair.** Independent protocol
review proved a cross-process ABA window that a marker recheck cannot close:
the bootstrap parent can spawn a still-unpublished child and crash, an
uninstaller can then create and clear its marker while removing the runtime,
and the delayed child can publish afterward. Closing the already-approved
bounded-bootstrap/runtime-retirement boundary requires the existing runtime
maintenance lock generation to remain kernel-claimed across `Popen` and daemon
publication, including parent-process loss. Under P0B-D21,
`src/vibecad/runtime/status.py` is added above only to expose the bounded lock
wait and inherited-claim validation used by bootstrap and `run_daemon`; the
deterministic regression remains in the already allowed
`tests/test_p0b_acceptance.py`. This adds no public operation, engine behavior
or product decision.

### 4. RED, GREEN and acceptance matrix

1. Prove the current MCP process still opens `AgentApplication` directly and
   the public local client/two-client acceptance module is absent.
2. Prove concurrent bounded bootstrap has one daemon authority, authenticated
   reuse and clean loser exit; daemon PID survives MCP EOF and a fresh
   MCP/Workbench connection.
3. Prove all stateful operations route through the daemon while unknown tools,
   invalid schemas, discovery and runtime controls do not open it.
4. Prove a sent-but-unanswered stateful request is not transport-retried;
   replay/list/get recovery returns one durable lineage after reconnect.
5. Prove descriptor import success, response-loss replay, restart and the full
   ancillary/symlink/hardlink/FIFO/directory/replacement/mutation negative
   matrix; original source bytes and identity remain unchanged.
6. Prove read-only resource success with exact URI/MIME/blob/hash/size and
   read-limit/error parity while a complete before/after tree snapshot records
   zero writes and no FreeCAD import.
7. Run M04 twice: one MCP-created review draft is observed and claimed by a
   fake Workbench, then Accept converges to one HEAD; a separate draft Reject
   converges with HEAD unchanged. Stale/revoked and cross-session grants fail.
8. Preserve exactly 28 tools and the frozen discovery projections; run the
   canonical C13 suite, direct-impact suites, full non-slow suite, real managed
   FreeCAD M04, full Ruff, exact changed-file format, compile, diff and fsck.
9. Require independent semantic, protocol/FD security and exact-diff reviews
   with Critical/Major zero before staging the named files and this ledger,
   committing `feat(agent): route MCP and Workbench clients through one kernel`,
   non-force pushing and verifying three-way SHA equality.

### 5. Circuit breakers and recovery

- If macOS `SCM_RIGHTS` cannot bind exactly one admitted request to exactly one
  descriptor without leak/reorder ambiguity, freeze C13 rather than accepting
  a client path or byte-size workaround.
- If ordinary client close, MCP EOF, runtime swap or uninstall can terminate
  or strand the daemon/Worker authority, freeze the affected lifecycle slice.
- If a read-only resource path writes, repairs, locks or silently skips corrupt
  state, remove it and retain the current daemon-backed export behavior until
  a zero-write reader passes.
- If any stateful MCP operation constructs a second Application/store/Worker,
  or if an uncertain mutation is retried automatically, C13 is NO-GO.
- Recovery anchor is the remotely verified C12 commit above. The separate
  research document is preserved untracked and must never be staged with C13.

## 29. Recovery Snapshot P0B-S16

### 1. Completed milestones

- C13 routes every stateful MCP and public fake-Workbench operation through
  one authenticated daemon-owned `AgentApplication` and Task Kernel. The MCP
  server no longer constructs an Application, Task/Revision store or CAD
  Worker; runtime maintenance controls and inert discovery remain local.
- `LocalAgentClient` is the shared public adapter. It performs pure
  request-envelope preflight, never retries an uncertain mutation, verifies
  exact Kernel API/package/build identity, reconnects without coupling client
  EOF to daemon lifetime, and lets each Workbench session mint and claim only
  its own file grant.
- Import from a public `source_path` is converted locally into exactly one
  `SCM_RIGHTS` descriptor plus a path-free identity locator. Protocol v2
  rejects paths, unexpected/truncated ancillary data and non-exact integer
  identities. The daemon duplicates and verifies the descriptor, then uses
  explicit-offset `pread`; a retained sender changing the shared open-file
  offset between every 7-byte chunk cannot alter the durable FCStd bytes,
  digest or size.
- The immutable artifact resource path now uses a strict read-only reader.
  It opens only existing private directories, records and published files,
  revalidates identity/size/hash bindings and performs no create, mkdir, lock,
  recovery, cleanup or FreeCAD work. No second writer exists.
- Runtime install/uninstall and daemon startup share one crash-safe
  maintenance generation. A bootstrap child inherits the exact lock claim
  through publication; an unproved losing child retains it until process exit.
  Pending uninstall blocks canonical MCP/Workbench admission and daemon
  publication. Authenticated retirement acknowledges before closing
  admission, drains every admitted request without a fixed execution cutoff
  and returns only after the exact daemon PID and publication disappear.
- P0B-M04 now creates project, task and review draft through the real MCP
  handler, including model-program validation, execution, FCStd checkpoint,
  STEP export and evidence collection. A second public client independently
  opens and claims the draft, then both clients observe identical task,
  revision, manifest, verdict and HEAD results across reconnect, Accept and
  Reject.
- Final controller gates are canonical C13
  `386 passed, 1 warning`, direct impact
  `526 passed, 1 deselected`, two consecutive M04 runs at `2 passed`, and
  two real managed FreeCAD Worker runs at `1 passed, 89 deselected`. The first
  full non-slow run had one transient observation of the later legal
  `CANCELLING` state in an unchanged active-cancel test; that test then passed
  four focused runs. The frozen second full run is
  `4890 passed, 107 deselected, 19 warnings in 314.28s`.
- The public surface remains exactly 28 tools. The SDK discovery projection is
  still `21,438` bytes with SHA-256
  `5d7703a55dd7b20c21c487d6f4740fbfb894cf6867c840ccb30adf57de63efda`;
  the complete frame is still `21,483` bytes with SHA-256
  `22c903b05fc6e46868bd74380880cca5c915f312ac2ddf24f7e48896b8cdf826`.
- Full changed-file Ruff, format and compile gates pass; `git diff --check`
  and `git fsck --no-dangling` pass. The 21-file allowlisted source/test
  content-manifest SHA-256 is
  `16965a7573434a491ad0bad7d884a88b22e19c64ea3223c53d099b84eaf40eaa`.
  Independent semantic/exact-diff and protocol/FD/lifecycle reviews are both
  GO with `Critical 0 / Major 0 / Medium 0`.

### 2. Next steps

1. Stage exactly the 21 frozen C13 source/test files plus this orchestration
   artifact. Do not stage `docs/CAD_AGENT_PRODUCT_RESEARCH.md`.
2. Commit as
   `feat(agent): route MCP and Workbench clients through one kernel`, perform
   the already-authorized immediate non-force push and verify exact
   `HEAD == @{upstream} == origin/codex/agent-stage3`.
3. Recover C14 from that remote anchor. Refresh the installed package, Skill
   projection and exact managed-runtime receipt together, then run the current
   managed public FreeCAD matrix. Do not revive the pre-C12 per-project
   executor-object identity assertion.

### 3. Approved decisions

- P0B-A01/A02 and P0B-D01..D22 plus D08A/D17A/D22-R1 remain active.
- MCP, Workbench and later external Agent integrations are clients of the one
  expert CAD Agent/Task Kernel. They do not receive a second scheduler,
  RevisionStore authority, Worker route or model-token entitlement.
- C13 closes P0B-RES-13 with a descriptor capability, not a path tunnel or
  generic filesystem tool. The same-UID local client remains inside the
  approved trust boundary; cross-platform transport remains P0B-RES-02.
- C14, not C13, owns package/version/Skill/managed-receipt refresh. The
  installed current-prefix receipt is intentionally not rewritten in this
  commit.

### 4. Execution discipline

- Freeze the reviewed 21 source/test files while this append-only ledger edit
  changes only the orchestration artifact. Use exact named-file staging and
  preserve the separate untracked research document.
- Continue with `native-plan / spawn-send-wait / repo-artifact /
  native-session-poll`, immediate non-force push and three-way local/upstream/
  remote equality. PR, tag, release, marketplace publication, force-push and
  external spend remain unauthorized.

## 30. Task Packet P0B-C14

### 1. Authorization

- This packet executes the already approved P0B-C14 row under P0B-R1.1,
  P0B-A01/A02 and P0B-D19/D20/D21/D22-R1. It inherits higher-priority system,
  developer and user instructions, applicable directory-scoped
  `AGENTS.md`/`CLAUDE.md`, the current host permission model and sandbox, and
  the narrowed file allowlist below. The Skill, artifact and packet cannot
  grant or expand permissions, elevate authority or bypass that model or
  sandbox.
- The user has already authorized autonomous continuation through P0-B and
  immediate non-force pushes of accepted commits. Do not ask for the same
  approval again. PR creation, tag, GitHub Release, PyPI/MCPB publication,
  marketplace mutation, force-push and external spend remain unauthorized.
- C14 is a local release-candidate/package and managed-runtime preservation
  gate. It does not claim a published release, real Claude/Codex host
  activation or a FreeCAD Qt Workbench UI.

### 2. Workspace anchor and exact allowlist

- Repository: `/Users/wangtao/Documents/DevProject/vibecad`.
- Branch: `codex/agent-stage3`.
- Entry anchor: `cf87fba0308f9a32820bf5237af61ea4e2d32989`.
  `HEAD`, upstream, the remote-tracking ref and
  `refs/heads/codex/agent-stage3` were all observed equal before this packet.
- The only pre-existing workspace item is the untracked
  `docs/CAD_AGENT_PRODUCT_RESEARCH.md`. It remains outside P0-B and must never
  be staged with C14.
- C14 may modify only:
  - `src/vibecad/__init__.py`
  - `src/vibecad/runtime/installer.py`
  - `src/vibecad/runtime/status.py`
  - `src/vibecad/worker/proxy.py`
  - `src/vibecad/worker/service.py`
  - `src/vibecad/runtime/spec.py`
  - `pyproject.toml`
  - `uv.lock`
  - `manifest.json`
  - `PRIVACY.md`
  - `README.md`
  - `skills/vibecad-agent/SKILL.md`
  - `skills/vibecad-agent/agents/openai.yaml`
  - `docs/ARCHITECTURE.md`
  - `docs/AGENT_ARCHITECTURE.md`
  - `docs/PRODUCT_CAPABILITY_ROADMAP.md`
  - `docs/USER_GUIDE.md`
  - `docs/ACCEPTANCE_TESTS.md`
  - `.github/workflows/release.yml`
  - `tests/test_agent_skill.py`
  - `tests/test_installer.py`
  - `tests/test_mcpb_manifest.py`
  - `tests/test_release_workflow.py`
  - `tests/test_runtime_integration.py`
  - `tests/test_status.py`
  - `tests/test_task_kernel_integration.py`
  - `tests/test_freecad_worker.py`
  - `tests/test_local_daemon.py`
  - this orchestration artifact.
- A listed file remains unchanged unless a genuine C14 RED requires it.
  Generated distributions may exist only in ignored `dist/` or fresh
  temporary roots. The existing managed runtime may change only inside the
  M05 maintenance/preservation gate.
- **P0B-C14-D21-01 — release identity allowlist repair.** The stage allowlist
  named package metadata but omitted `uv.lock`; changing the root package from
  0.5.0 to 0.6.0 without its frozen lock would make `uv sync --frozen`, wheel,
  sdist and MCPB disagree. P0B-D21 therefore adds only `uv.lock`.
- **P0B-C14-D21-02 — packaged privacy allowlist repair.** MCPB fresh-unpack
  acceptance requires byte parity for `PRIVACY.md`, which is included at the
  package root and still names 0.5.0. P0B-D21 therefore adds only
  `PRIVACY.md`; the privacy boundary itself is unchanged.
- **P0B-C14-D21-03 — runtime identity and release-gate repair.**
  `src/vibecad/runtime/spec.py` is directly version-derived and
  `tests/test_status.py` owns its strict receipt contract. The release
  workflow and its tests may change only if the built-artifact RED proves that
  a publish job can consume an untested wheel/sdist/MCPB candidate. These are
  packaging/verification repairs inside P0B-D20, not new product capability.
- **P0B-C14-D21-04 — shipped documentation consistency.** Read-only inventory
  proved that `docs/USER_GUIDE.md` and `docs/ACCEPTANCE_TESTS.md` still freeze
  the 0.5.0/27-tool/C05 package boundary. They are direct user and acceptance
  projections of the already-approved C14 release contract, so P0B-D21 adds
  only these two documents. Their update may describe the completed C13
  backend but may not claim a Qt Workbench UI, tag or external publication.
- **P0B-C14-D21-05 — Worker import protocol allowlist repair.** The exact
  managed public matrix proved that `import_fcstd` copies an admitted source
  to the private `.work.<intent>.FCStd` name defined by the durable project
  service, while the parent and child Worker validation protocol allowlists
  admit only `.import`, `.stage` and `.normalized`. The same supported
  Box/Cylinder FCStd passes `InProcessCadExecutor.validate_import` but is
  rejected by `WorkerCadExecutionPort` before FreeCAD with `invalid_input`.
  P0B-D21 therefore adds only `src/vibecad/worker/proxy.py`,
  `src/vibecad/worker/service.py` and `tests/test_freecad_worker.py` to admit
  the already-private `.work` basename through the descriptor-bound validation
  method. Absolute paths, arbitrary basenames and every descriptor, identity,
  mutation and generation-fencing check remain unchanged.
- **P0B-C14-D21-06 — managed acceptance child import repair.** After D21-05
  admitted the real Worker import path, the existing public-matrix child
  stopped at `create_task` because its isolated source string calls
  `secrets.token_hex` without importing `secrets`; the outer pytest module's
  import is intentionally unavailable to the child interpreter. The affected
  test file is already allowlisted. Adding that one standard-library import
  repairs only the acceptance harness and changes no product or public
  contract.
- **P0B-C14-D21-07 — exact same-version release-maintenance refresh.** D21-05
  changed the final wheel after the reviewed managed prefix had already
  installed an earlier local `0.6.0` candidate. The normal installer correctly
  short-circuits a CURRENT receipt, and pip `--upgrade` correctly skips another
  wheel with the same version; therefore neither path can prove that the final
  reviewed wheel bytes are installed. P0B-D21 adds only
  `src/vibecad/runtime/installer.py` and `tests/test_installer.py` (runtime
  status and the M05 test are already allowlisted) for one internal,
  release-maintenance-only transaction. It requires the default managed
  prefix, exact CURRENT/engine evidence, canonical wheel name and caller-bound
  SHA-256; returns idempotently when installed sources already match; otherwise
  revokes the exact CURRENT receipt before a capability-bound
  `pip install --no-index --force-reinstall --no-deps` and republishes it only
  after exact source parity plus runtime verification. It is not exposed
  through MCP, normal `ensure_runtime`, launcher auto-update or the 28-tool
  surface. It provides no automatic general crash-resume: a hard crash after
  receipt revocation leaves a safe non-CURRENT runtime that this CURRENT-only
  seam cannot re-admit. A retained reviewed wheel and maintenance authority are
  recovery inputs for an explicit controller repair, not automatic resume.
- **P0B-C14-D21-08 — M05 reviewed-prestate and protection-order repair.**
  Independent release review found that M05 captured engine/data baselines
  only after daemon retirement, used lower bounds instead of the packet's
  reviewed engine manifest, and accepted any same-named wheel. The existing
  M05 test is therefore tightened inside the maintenance authority to bind the
  exact reviewed engine manifest and either the exact 0.5.0 entry receipt or
  exact current receipt, require an independently supplied final-wheel
  SHA-256 before resolving or mutating, and snapshot every durable project
  subtree before retirement. Retirement may change only its explicit
  `data/daemon` and lock authority; protected data, legacy, external binding,
  views, engine and prefix identity must match both after retirement and after
  refresh.
- **P0B-C14-D21-09 — per-document release-truth regression.** Independent
  documentation review found that the combined-corpus assertion could let one
  correct document hide another document regressing to 27 tools, delivered Qt
  UI or a published-release claim. The already allowed README and Agent Skill
  test are tightened per applicable document for 28 tools, shared
  daemon/Task Kernel, G1 not delivered and no tag/release; README's stale
  `P0-B core -> G1` sentence is corrected to start with the 0.6.0
  package/managed-runtime closeout. Product scope is unchanged.
- **P0B-C14-D21-10 — stable engine identity versus live mutable byte
  preservation.** The first final M05 invocation stopped before daemon
  retirement or package mutation because the reviewed engine manifest changed
  from `aa72...` to `68b0...` while its 14,756 entries, 511,110,973 regular
  bytes and 117 symlinks remained exact. Comparing the current conda-owned
  files with their conda package-record `sha256_in_prefix` values isolated five
  regenerated CPython bytecode files: four owned by FreeCAD and one by Python;
  their individual sizes and digests differ from the conda records even though
  the observed full-tree total happened to remain unchanged. OCCT and every
  non-`.pyc` engine file remained exact. A single
  hard-coded live-tree digest is therefore not a stable package-identity
  precondition. M05 therefore splits the proof: a stable digest
  `702776db54c7532d71e725cde2099b6d81a9a3d22d139a5a28bd0f93bce3d261`
  pins exact conda records plus all 12,426 immutable entries and 432,421,415
  immutable regular bytes; all 2,330 excluded `.pyc` entries must have a
  package-recorded `.py` source. A separate full live digest continues to hash
  every current byte across 14,756 entries. The stable digest is bound at
  admission, and the complete returned manifest including the live digest must
  remain byte-for-byte equal immediately after daemon retirement and after
  package refresh. This removes the false positive without allowing refresh to
  change even one mutable cache byte.
- **P0B-C14-D21-11 — shipped release state, cancellation and documentation
  link repair.** Independent product/package review found three contradictory
  shipped projections: release-candidate docs still described C14 as
  executing after the candidate gates complete; Agent architecture told a
  host only to read an active cancellation even when the canonical
  `next_action=reconcile` contract requires one generation-bound
  `resume_task`; and the packaged README used relative links to `docs/` even
  though wheel/MCPB intentionally omit that tree. The already allowed docs
  will state that the local 0.6.0 candidate is complete but unpublished,
  preserve the exact read-then-one-resume cancellation rule, and use canonical
  repository URLs for extended source documentation. No runtime or product
  scope changes.
- **P0B-C14-D21-12 — publishers must consume the tested final artifacts.**
  Independent release review found that the tag workflow ran macOS FreeCAD
  gates from the checkout before building wheel/sdist/MCPB, then allowed
  publishers to consume newly built artifacts that had never been installed
  or launched. Local C14 evidence proves only this checkout and cannot repair
  future tag executions. The already conditional release workflow and its
  regression test will therefore build and audit artifacts after the version
  and quality guards, fresh-install both wheel and sdist, compare packaged
  Python sources across wheel/sdist/MCPB, then make the macOS managed Agent
  matrix download and install that exact wheel. One matrix leg will also
  fresh-unpack that exact MCPB and run its real stdio/resource acceptance
  against the managed engine. PyPI and GitHub Release jobs must depend on this
  downstream macOS consumer gate and may only download the previously gated
  artifacts; they still do not rebuild or publish from the checkout.
- **P0B-C14-D21-13 — real managed forward-revert acceptance.** Independent
  product review found that `revert_project`, the 28th public tool and the
  principal P0-B recovery writer, had deterministic API/daemon/fake-CAD
  coverage but neither declared managed FreeCAD release target executed it.
  The 28-tool discovery claim therefore exceeded the real-CAD evidence for
  this one path. The already allowed managed Task Kernel integration test will
  add committed ancestry followed by a verified forward-revert draft,
  immutable draft inspection and explicit Accept publication; the existing
  deterministic Reject/HEAD-unchanged tests remain the complementary branch.
  User and acceptance guidance will describe this forward-draft contract and
  name its real managed gate. This closes evidence for an approved capability;
  it does not add a new tool or broaden CAD semantics.
- **P0B-C14-D21-14 — maintenance seam wording precision.** Final runtime
  review found no Critical/Major/Medium issue but noted that two internal
  docstrings could overstate the implemented boundary: the refresh proves
  exact Python-source parity plus wheel provenance rather than arbitrary
  future package-data bytes, and the public Python maintenance method requires
  its release controller to retire the local Kernel before invocation. The
  already allowed runtime docstrings will state those exact facts. Behavior,
  API exposure and authorization remain unchanged.
- **P0B-C14-D21-15 — real-revert acceptance status spelling.** The first
  D21-13 managed run completed the real revert, immutable draft export,
  FCStd/STEP reload and Accept path, then failed only because the new parent
  assertion used `awaiting_review` instead of the frozen public TaskStatus
  value `awaiting_user_review`. The already allowed integration test will
  correct that literal and rerun the identical managed target; no product code
  or state transition changes.
- **P0B-C14-D21-16 — daemon handshake version projection.** The first final
  non-slow run reached 4,901 passing tests and one failure: the real daemon
  correctly advertised package version `0.6.0`, while
  `tests/test_local_daemon.py` still froze `0.5.0`. This is a direct
  verification projection of the already approved C14 identity, not a daemon
  behavior change. P0B-D21 adds only that test file, replaces the stale
  expected value with `0.6.0`, and requires the focused daemon test plus the
  final full suite to pass.
- **P0B-C14-D21-17 — descriptor-leak assertion isolation.** The next full
  suite had 4,901 passing tests and one Darwin ancillary-data test failure
  because the process-wide `/dev/fd` count decreased from 140 to 139 while the
  daemon test was running. A decreasing global count cannot indicate a leaked
  received descriptor; it proves an unrelated concurrent descriptor closed
  and makes exact equality nondeterministic in this multithreaded suite. The
  already allowed daemon test will instead snapshot descriptors whose
  device/inode exactly matches the imported source and require that exact set
  after the truncated ancillary connection drains. This directly detects any
  leaked received source descriptor while ignoring unrelated descriptor
  churn. Daemon behavior is unchanged.
- **P0B-C14-D21-18 — runtime receipt seam allowlist repair.** Final settled
  review found that the exact C14 list omitted
  `src/vibecad/runtime/status.py` even though D21-07 requires its
  evidence-bound CURRENT receipt revocation before any same-version package
  mutation. D21-07 incorrectly said that runtime status was already
  allowlisted. P0B-D21 therefore adds only this directly required receipt seam
  without rewriting the earlier record; its change remains limited to exact
  revocation and truthful maintenance wording, with focused status/installer
  and M05 evidence already recorded.
- **P0B-C14-D21-19 — domain-call topology wording.** Final semantic review
  found that README's `MCP transport or daemon -> Application` wording could
  imply two direct stateful routes, while the implementation routes
  project/task/revision/review/artifact/CAD domain calls from MCP and the
  public Workbench client through the same authenticated daemon and single
  `AgentApplication`. Runtime maintenance and inert discovery remain local MCP
  server concerns. The already allowed README and Skill will name that exact
  split; no execution path changes.
- **P0B-C14-D21-20 — active-cancel observation race.** A later full run again
  reached 4,901 passes and one known C12 test race: after
  `cancel_task` returned `recovery_required`, the submitting thread advanced
  the durable task from `cancel_requested` to the equally valid
  `cancelling` state before the test's immediate observation. The same test
  already accepts all three active-cancel states at its converged observation
  and verifies the exact ordered cancel transitions, Worker death, unchanged
  HEAD/source and clean next generation. No implementation change is
  justified; the focused test will be repeated before one final full run.
- **P0B-C14-D21-21 — active-cancel test contract correction.** Eight focused
  reruns happened to pass, but final semantic review correctly rejected D21-20
  as insufficient: rerunning cannot close a legal race that two full suites
  have observed. The already allowed Worker tests will accept either
  `cancel_requested` or `cancelling` at the immediate post-error read. They
  still require exactly one cancel-request transition, the same first Worker
  generation and no replacement Worker; live port ownership is required while
  still `cancel_requested`, while `cancelling` may already have atomically
  detached that dead generation. Final assertions remain exact for ordered
  `request_cancel -> start_cancellation -> confirm_cancelled`, Worker/process
  death, unchanged HEAD/source/candidate cleanup and a clean succeeding next
  generation. This changes only the observation contract, not cancellation
  behavior.
- **P0B-C14-D21-22 — final artifact regeneration after topology wording.**
  D21-19 changed packaged README and canonical Skill bytes after the earlier
  `4b322e...` wheel candidate, so that candidate and its M05 provenance became
  stale even though Python sources did not change. The final fresh build root
  therefore regenerated and re-audited all four channels. Final SHA-256 values
  are wheel
  `3c73451aa6fd209e7e4877abad6fba0200ff97a8f6bbca45c5e4a4d5ab31014d`,
  sdist
  `4fc514cd49815e92c213686fcbdfe0847e651a2502baf8d68f264b4fc6e1aa83`,
  MCPB
  `1eb2f468cc9995da330cc8e6511a40e68eae04be90657e1f8f00c0beb8b9b1cc`
  and standalone Skill
  `db27e09408a0fbe8e3a275c53bf88ffad1dd60c1adf7dfe36e03ca8f9622de28`.
  Archive/RECORD/Python/Skill parity, fresh wheel/sdist installs and fresh MCPB
  stdio/resource acceptance passed. M05 was then repeated against this exact
  wheel and passed in 264.13 seconds with prefix/engine/data unchanged.

### 3. Context and frozen product outcome

- C13 completed and pushed the one daemon-owned Task Kernel path for MCP and a
  public fake-Workbench client. The backend supports reconnectable project,
  task, revision, draft, Accept/Reject, FCStd/STEP resource and Worker
  isolation flows. The real FreeCAD Qt Workbench UI remains G1.
- C14 packages that exact backend as local candidate version `0.6.0`.
  Runtime epoch remains integer `4`; Python remains `3.12`, FreeCAD remains
  `1.1.0`, MCP remains `1.27.2`, the public surface remains exactly 28 tools,
  and canonical public-surface SHA-256 remains
  `ae495ba457af40a5837a03e77eef4b396b0a4209755878350bc341ac7de8bfd3`.
- Discovery remains exactly 21,438 bytes for the SDK projection and 21,483
  bytes for the complete frame, below the 32 KiB budget. C14 changes package
  identity and truthful guidance, not public names, schemas, annotations or
  CAD semantics.
- At C14 packet entry, the managed prefix was
  `/Users/wangtao/Library/Application Support/VibeCAD/runtime/mamba/envs/vibecad`
  at device/inode `16777221/14014428`. It contains Python 3.12.13, FreeCAD
  1.1.0 and OCCT 7.9.3, but its installed VibeCAD package and receipt are
  0.5.0 with stale 20-tool surface
  `84b6abe8c1b496153ed2be083e1ea3186f642c47d9dfa9c2f90f66e92e6139f9`.
  Therefore pre-C14 `runtime_receipt_state=server_mismatch`,
  `runtime_ready=false` and full server verification fails. This is a stale
  server package, not a missing FreeCAD engine.
- At packet entry, the receipt SHA-256 was
  `952601b2c6943746dd5ebc72ea8d33655f86f584249d5feb2eb508fe5df11f52`.
  With only the approved version change, the expected canonical 0.6.0 receipt
  is 274 bytes and SHA-256
  `b154e2189adaf718a9231aef30972e25774e20d4d888aa5f4e95520793d64fbd`.
- After the final M05 gate, the same prefix remains at device/inode
  `16777221/14014428`; it is CURRENT and runtime-ready with the exact canonical
  0.6.0 receipt above. Installed `direct_url.json` binds final wheel SHA-256
  `3c73451aa6fd209e7e4877abad6fba0200ff97a8f6bbca45c5e4a4d5ab31014d`,
  and all 97 installed Python sources match that wheel.
- The default durable `data/` root is absent at entry. Existing legacy
  runtime, external binding and 64 historical `views/` files are out of the
  replacement set and must stay byte-identical. The stable engine-owned conda
  manifest covers 12,426 immutable entries, 432,421,415 immutable regular
  bytes and 117 symlinks with SHA-256
  `702776db54c7532d71e725cde2099b6d81a9a3d22d139a5a28bd0f93bce3d261`.
  The full live manifest covers 14,756 entries; its observed 511,110,973 bytes
  and `68b02e...` digest are not frozen across ordinary Python use because all
  2,330 package-recorded `.pyc` files may be regenerated. Every mutable
  bytecode entry has a package-recorded `.py` source, and the complete live
  manifest must still remain equal across the M05 retirement/refresh
  transaction.

### 4. Steps and objective gates

1. Update focused contract tests first and capture a genuine RED for 0.6.0,
   28-tool Skill wording, current C12 cancellation/reconcile semantics,
   truthful daemon/Workbench backend status, frozen lock identity and
   built-artifact release gating. Correct the stale slow assertion so separate
   project runtime/coordinator/session objects share the one
   application-owned Worker executor.
2. Change only the four synchronized release identities
   (`pyproject.toml`, package `__version__`, manifest and `uv.lock`) to 0.6.0.
   Keep epoch, dependency pins and public-surface digest unchanged.
3. Refresh Skill, README, Privacy, user/acceptance guidance and
   architecture/roadmap wording. State that the authenticated daemon backend
   and fake-Workbench client are complete, while the FreeCAD Qt Workbench UI,
   face/edge selection, STEP/STL import, reverse reconstruction, photo
   reconstruction and simulation are not.
4. Run the focused GREEN suites, exact version guard for `v0.6.0`, surface
   count/byte/hash assertions, Ruff, exact changed-file formatting, compile
   checks and `git diff --check`.
5. In a fresh output root build and audit wheel plus sdist, validate and pack
   MCPB 2.1.2, build the deterministic Skill archive, audit safe archive paths,
   wheel RECORD, source/Skill byte parity and artifact hashes, and install the
   wheel into a fresh isolated Python environment. The fresh install must
   report 0.6.0, epoch 4, MCP 1.27.2, surface digest `ae495...` and 28 tools.
6. Freshly unpack the final MCPB and run its real stdio/resource acceptance
   against the exact managed FreeCAD runtime. The package must be byte-exact to
   the checkout contract and may not import from the checkout through an
   editable path.
7. Run M05 using only the final wheel as `VIBECAD_PIP_SPEC`, with
   `PIP_NO_INDEX=1`, no `VIBECAD_FREECAD_ENV`, and the existing runtime
   maintenance authority. Retire any exact local Kernel as `runtime_upgrade`,
   then let `RuntimeInstaller` select only its engine-compatible
   `installing_pip -> verifying -> ready` path. A create/remove/download phase
   or failed engine proof is a circuit breaker.
8. After M05 prove: prefix device/inode unchanged; the stable 12,426-entry
   immutable engine identity remains exact and the complete 14,756-entry live
   engine manifest is unchanged; legacy prefix, external binding, historical
   views and all project/task/revision bytes unchanged; receipt equals the 274-byte
   expected value; installed source matches the final wheel; state is
   `CURRENT`; `runtime_ready` and `verify_runtime` are true; public surface is
   28 tools.
9. Run:
   - `PYTHONPATH=src .venv/bin/python -m pytest -q`
   - the declared slow Worker/P0-B acceptance suite;
   - the current managed task-kernel candidate and public Agent matrix;
   - the fresh MCPB stdio acceptance;
   - full Ruff, exact changed-file format/compile, diff and
     `git fsck --no-dangling`.
10. Obtain separate package/release, runtime-preservation and semantic/docs
    reviews. Critical/Major/Medium must be zero before exact named-file staging,
    commit `chore(release): package P0-B core as 0.6.0`, immediate non-force
    push and exact local/upstream/remote equality.

### 5. Execution discipline and circuit breakers

- Capability profile:
  `native-plan / spawn-send-wait / repo-artifact / native-session-poll`.
- Required evidence categories:
  - `live capability declarations`: `update_plan`, `spawn_agent`,
    `send_message`, `wait_agent`, `exec_command` and `write_stdin` are declared
    in this session.
  - `observable behavior`: subagents returned bounded read-only audits;
    commands completed synchronously and prior long gates returned pollable
    native sessions.
  - `environment identity`: Codex desktop on the declared macOS workspace,
    branch and executable paths above.
  - `public configuration`: repository branch/upstream and public package
    metadata were inspected read-only; no other capability evidence observed.
- Exact executable evidence at entry: Homebrew `uv 0.11.28` at
  `/usr/local/bin/uv`; Node `v26.5.0` at `/usr/local/bin/node`; npx `11.17.0`
  at `/usr/local/bin/npx`; controller Python 3.13.14 at `.venv/bin/python`;
  managed Python 3.12.13 at the prefix above; FreeCAD reports 1.1.0.
- Use `native-session-poll` for long suites and package gates; keep polling
  the original session and never relaunch it. Use exact named-file staging,
  never `git add .` or `git add -A`.
- Stop on an unexpected RED, out-of-allowlist write, changed 28-tool contract,
  discovery above 32 KiB, package-channel mismatch, engine-manifest change,
  runtime rebuild path, user-data mutation, unverified receipt, unauthorized
  publication or commit-budget overflow. Preserve evidence and recover from
  `cf87fba...`; do not repair around a breaker.

### 6. Delivery boundary

- C14 is complete only when one reviewed 0.6.0 candidate commit is pushed,
  package artifacts have reproducible hashes, the exact installed managed
  candidate is CURRENT, real FreeCAD public flows pass, and the ledger plus a
  four-section recovery snapshot record all evidence.
- The controller alone accepts review findings, stages named files, commits,
  pushes and verifies remote equality. C15 remains a separate orchestration
  closeout commit.
- Tag/release/publication, real external host activation and G1 Workbench UI
  work remain outside C14.

### 7. Required final report

- Record the exact commit and push SHA, artifact filenames/sizes/SHA-256,
  package and Skill parity, tool count/discovery bytes, focused/full/slow/E2E
  counts, managed-runtime before/after identities and manifests, receipt hash,
  independent review severities, residuals and final workspace state.
- Record any justified deviation beside its RED and D21 entry. Preserve the
  excluded research document unchanged and report `none` rather than leaving
  evidence fields ambiguous.

## 31. Recovery Snapshot P0B-S17

### 1. Completed milestones

- C14 packages the P0-B backend as candidate version `0.6.0` across Python,
  project metadata, manifest, lock file, README, Privacy, architecture,
  acceptance guidance and the standalone Skill. Runtime epoch remains `4`;
  Python remains `3.12`, FreeCAD remains `1.1.0`, MCP remains `1.27.2`.
- The public surface remains exactly 28 tools with canonical contract SHA-256
  `ae495ba457af40a5837a03e77eef4b396b0a4209755878350bc341ac7de8bfd3`.
  SDK discovery remains `21,438` bytes and the complete frame remains `21,483`
  bytes, both below the 32 KiB budget. Project, task and CAD domain operations
  still converge on the one daemon-owned `AgentApplication` and Task Kernel;
  release maintenance and inert discovery remain local server concerns.
- The Worker import protocol admits only the exact private
  `.work.<id>.FCStd` staging form needed by the real managed path. The public
  matrix now covers create/mutate/checkpoint/STEP/evidence, independent
  cross-process review, Accept/Reject and historical forward-only revert:
  verified immutable draft, FCStd/STEP reload, Accept and a new forward
  commit without rewriting prior history.
- Exact same-version release maintenance is fail-closed and private to the
  default CURRENT managed prefix. It validates the canonical wheel name and
  caller SHA before revoking the receipt, force-reinstalls with
  `--no-index --no-deps`, verifies all installed Python sources and
  `direct_url.json` provenance, then commits the receipt before returning
  READY. A rejection before receipt revocation preserves the healthy CURRENT
  prefix; once revocation has occurred, any later failure leaves the receipt
  revoked for explicit repair.
- Final M05 refreshed the exact final wheel in place at
  `/Users/wangtao/Library/Application Support/VibeCAD/runtime/mamba/envs/vibecad`.
  Prefix device/inode remains `16777221/14014428`; the 12,426-entry,
  432,421,415-byte immutable engine identity remains
  `702776db54c7532d71e725cde2099b6d81a9a3d22d139a5a28bd0f93bce3d261`;
  the complete 14,756-entry live manifest and protected data remained equal.
  State is CURRENT and runtime-ready with the canonical 274-byte receipt
  SHA-256
  `b154e2189adaf718a9231aef30972e25774e20d4d888aa5f4e95520793d64fbd`.
- Final reproducible candidate artifacts are:
  - `vibecad-0.6.0-py3-none-any.whl`: 599,337 bytes, SHA-256
    `3c73451aa6fd209e7e4877abad6fba0200ff97a8f6bbca45c5e4a4d5ab31014d`;
  - `vibecad-0.6.0.tar.gz`: 639,203 bytes, SHA-256
    `4fc514cd49815e92c213686fcbdfe0847e651a2502baf8d68f264b4fc6e1aa83`;
  - `VibeCAD.mcpb`: 703,655 bytes, SHA-256
    `1eb2f468cc9995da330cc8e6511a40e68eae04be90657e1f8f00c0beb8b9b1cc`;
  - `vibecad-agent-skill-0.6.0.zip`: 4,116 bytes, SHA-256
    `db27e09408a0fbe8e3a275c53bf88ffad1dd60c1adf7dfe36e03ca8f9622de28`.
  Archive paths, wheel RECORD, all 97 Python sources, source manifest and Skill
  parity passed. Fresh wheel and sdist installs plus fresh MCPB unpack,
  stdio/resource and FCStd/STEP acceptance passed.
- Final controller evidence is full non-slow
  `4902 passed, 108 deselected, 19 warnings in 201.06s`; slow Worker/P0-B
  `11 passed, 102 deselected in 91.76s`; final managed candidate/public matrix
  `2 passed in 18.39s`; fresh MCPB acceptance `1 passed in 11.76s`; exact final
  wheel M05 `1 passed in 264.13s`; and the mechanically reformatted Skill
  projection test `12 passed`. Full Ruff, exact 14-file format and compile,
  offline lock, version, release-workflow YAML, diff and fsck gates pass.
  Package/release, runtime-preservation and final semantic/docs reviews are GO;
  the independent final signature is
  `Critical 0 / Major 0 / Medium 0 / Minor 0`.

### 2. Next steps

1. Stage exactly the 27 tracked C14 files named by the packet and this
   snapshot; do not stage `docs/CAD_AGENT_PRODUCT_RESEARCH.md`.
2. Commit as `chore(release): package P0-B core as 0.6.0`, perform the
   already-authorized immediate non-force push, and verify exact
   `HEAD == @{upstream} == origin/codex/agent-stage3`.
3. Use the C14 remote commit as the recovery anchor. C15 may record final
   orchestration closeout; G1 Qt Workbench interaction and later reconstruction
   or simulation phases require their own bounded task packets.

### 3. Approved decisions

- P0B-A01/A02, P0B-D01..D22 plus D08A/D17A/D22-R1 and
  P0B-C14-D21-01..22 remain active.
- Claude, Codex and future Agent hosts are clients of this expert CAD Agent.
  They discover the 28-tool contract and receive durable FCStd/STEP resources,
  but do not receive a second scheduler, Worker route, storage authority or
  model-token entitlement.
- All project, task, revision, review, artifact and CAD-domain calls enter the
  authenticated daemon and its one `AgentApplication` and Task Kernel.
  CAD/model mutations specifically use the project lock, immutable revision
  evidence, review draft, explicit Accept/Reject and forward-only history.
  Runtime ensure and uninstall remain MCP-local maintenance; exact
  same-version refresh is a controller-only private release-maintenance seam.
  Neither maintenance path enters the Task Kernel, and refresh is not a 29th
  public tool.
- Mutable package-owned `.pyc` files are excluded from stable engine identity
  only when their exact `.py` sources are package-recorded. Complete live
  manifest equality remains mandatory across one M05 transaction.
- This candidate does not claim a delivered Qt Workbench UI, interactive
  face/edge selection, STEP/STL reverse reconstruction, photo reconstruction,
  simulation, public tag, release or marketplace publication.

### 4. Execution discipline

- Freeze the reviewed 27 tracked files after this append-only snapshot. Use
  exact named-file staging; preserve the separate untracked research document
  byte-for-byte.
- Recovery capability profile remains
  `native-plan / spawn-send-wait / repo-artifact / native-session-poll`.
  Its declared adapters are `update_plan`, `spawn_agent`, `send_message`,
  `wait_agent`, `exec_command`, `write_stdin` and `apply_patch`; long gates
  must resume their original pollable session rather than be relaunched.
- On recovery, first verify the declared repository/branch, exact
  `HEAD`/upstream/remote-tracking equality, named-file status and excluded
  research hash
  `ada5049d80b8914c43d711649feeb968ec7c83f4a6a9846d399a431b09ee856e`.
  Then verify the four artifact hashes, managed prefix device/inode, CURRENT
  receipt/direct-wheel provenance and public 28-tool digest before resuming.
  Re-run changed-file Ruff/format/compile, offline lock, version, YAML,
  `git diff --check`, `git fsck --no-dangling`, focused affected suites and
  the full/slow/real-FreeCAD gates in proportion to any recovered change.
- Circuit breakers remain: an out-of-allowlist write; unexpected test RED;
  changed tool name/schema/annotation or public digest; discovery above
  32 KiB; package-channel or artifact-hash mismatch; runtime
  create/remove/download instead of the admitted in-place refresh; changed
  prefix, immutable engine identity, complete live manifest or protected user
  data; unverified receipt; unauthorized publication, force-push, external
  activation or spend. Stop and recover from the last three-way-equal remote
  anchor instead of repairing around any breaker.
- The final artifact root is
  `/private/tmp/vibecad-c14-final4.oj3rHb`; it is evidence, not a tracked
  publication channel. No tag, GitHub release, marketplace publication,
  force-push, external host activation or external spend is authorized.
- The residual ledger at Section 8 remains authoritative. P0B-RES-01..12 are
  active: real host activation; Linux/Windows process semantics; the same-UID
  trust boundary; already-opened FreeCAD memory; semantic geometry diff;
  cross-store GC; private-runner digest migration; complete durable telemetry;
  streamed artifacts above the 64 MiB buffered limit; malicious-code
  sandbox/remote pool; G1 Qt Workbench; and publication authority. For
  P0B-RES-12, only the explicitly authorized branch-push portion closes after
  this push; PR, tag, release and marketplace publication remain open.
  P0B-RES-13 is closed by C13's descriptor-bound import. P0B-RES-14 remains
  active for the private revert-preparation ownership split.
- C14-specific non-blocking residuals are also explicit: a hard crash after
  receipt revocation requires an explicit repair rather than automatic resume;
  installed-package proof currently covers exact Python sources plus wheel
  provenance; the live M05 default data root was absent, while non-empty
  durable data preservation is covered by separate tests.
- Until the push succeeds, recovery remains the verified C13 remote anchor
  `cf87fba0308f9a32820bf5237af61ea4e2d32989`. After push, the new three-way
  equal C14 commit becomes the only continuation anchor.

## 32. Task Packet P0B-C15

### 1. Authority and exact scope

- P0B-A01 explicitly approved P0B-C00 through P0B-C15 at P0B-R1, including
  the stage decisions, allowlist, gates, budgets, exclusions and local
  commit/no-publication policy. P0B-A02 and P0B-D22-R1 authorize the immediate
  non-force push of each accepted commit on `codex/agent-stage3`.
- C15 is a documentation-only stage-closeout packet. It may modify only this
  orchestration artifact. It may not change source, tests, dependencies,
  package artifacts, the managed runtime, user data, public tools, product
  behavior or repository configuration. The only permitted external change is
  the one current-branch non-force push explicitly authorized by
  P0B-A02/P0B-D22-R1.
- PR, tag, release, marketplace publication, force-push, real-host activation,
  external model use and external spend remain unauthorized.

### 2. Entry context and append-only corrections

- Entry branch is `codex/agent-stage3`. Before this packet,
  `HEAD`, upstream, remote-tracking and the actual remote branch are all
  `157d33f89386499dfbf3d589cd8a57ffffcde434`, the pushed C14 commit
  `chore(release): package P0-B core as 0.6.0`.
- At initial C15 entry no tracked file was dirty. The only workspace item was
  the deliberately excluded untracked `docs/CAD_AGENT_PRODUCT_RESEARCH.md`,
  SHA-256
  `ada5049d80b8914c43d711649feeb968ec7c83f4a6a9846d399a431b09ee856e`.
- While C15 was being drafted, a separate user-owned strategy task committed
  `7eb4b3a92a937e005509d75b8d6b111b134a9350`
  (`docs(strategy): consolidate product and backend direction`) with exactly
  five strategy/research paths and explicitly excluded this orchestration
  artifact. It was independently completed but left local; the existing
  current-branch push authority was then used to push that commit separately.
  `HEAD`, upstream, remote-tracking and the actual remote are now all
  `7eb4b3a92a937e005509d75b8d6b111b134a9350`. This interleaved commit is not a
  P0-B packet or repair commit and does not expand C15's one-file allowlist.
- That separate strategy commit intentionally began tracking and revised
  `docs/CAD_AGENT_PRODUCT_RESEARCH.md`; its current SHA-256 is
  `53f75ba475db9b1d3d83e64651a77993b3a6bf5d5a0470ef912193dc33d55deb`.
  The original P0-B exclusion was honored because the file was not staged in
  C14 and is not changed by C15. After the strategy push, the only C15
  execution-workspace diff is this orchestration artifact.
- P0B-S17 is correctly preserved as a pre-C14-push snapshot. C15 supersedes,
  rather than rewrites, its now-stale next steps and temporary C13 recovery
  anchor with the observed C14 push and equality above.
- P0B-E17's observable `this C14 commit / immediate non-force push required`
  field is superseded by the actual C14 commit
  `157d33f89386499dfbf3d589cd8a57ffffcde434`, its successful non-force push and
  the verified four-way equality that existed before the strategy task.
- P0B-S17 says that the residual ledger is in Section 8. The authoritative
  residual table is actually Section 9; C15 records this correction without
  editing the historical snapshot.
- The first C15 artifact-readback shell command used `path` as a zsh loop
  variable. Because `path` is zsh's tied special parameter for `PATH`, the
  command exited 127 when the next executable could not be found. The corrected
  read-only command used `artifact_file`, called `/usr/bin/stat` explicitly and
  reproduced all four C14 sizes and hashes. This was a command-construction
  error, not a product, artifact or gate RED.

### 3. Closeout decisions

- **P0B-C15-D01 — implementation end versus closeout envelope.** The final
  product/package commit is C14 at `157d33f...`. C15 is the planned
  documentation-only closeout envelope. A Git commit cannot contain its own
  final object ID, so `SELF(P0B-C15)` means the unique commit whose sole parent
  is `7eb4b3a92a937e005509d75b8d6b111b134a9350`, whose exact subject is
  `docs(orchestration): close P0-B core delivery`, whose diff path is exactly
  `docs/orchestrated/vibecad-p0b-core.md`, and whose tree contains P0B-E18,
  P0B-E19, P0B-X01 and P0B-S18. Completion additionally requires
  `HEAD == @{upstream} == remote-tracking == git ls-remote == SELF` after the
  authorized non-force push. The controller reports the resolved SHA; if any
  predicate fails, C15 is blocked, `7eb4b3a...` remains the branch recovery
  anchor and C14 `157d33f...` remains the product/package end.
- **P0B-C15-D02 — completion accounting.** P0B-C00 through P0B-C14 are
  committed and pushed. The commit containing this packet's final ledger and
  recovery snapshot completes P0B-C15, making all 16 planned packet IDs
  terminal. P0-B then consumes 17 of the 20-commit hard budget: 16 packet
  commits plus the authorization-only amendment
  `a7e6881e21936dc5b5dc2c92f5c1dd70b9498dfe`. Additional repair commits are
  used `0`; the original authorization allowed up to 4 but the hard total has
  only 3 slots remaining after C15. The numerous gate/review repairs were
  completed inside their owning packet commits and remain recorded beside the
  original evidence and D21 decisions. The interleaved strategy commit is
  outside the P0-B campaign; the branch has 18 post-Stage-3 commits after
  `SELF(P0B-C15)` exists.
- **P0B-C15-D03 — residual disposition.** P0B-RES-01..11 and P0B-RES-14 remain
  active. P0B-RES-12's branch-push portion is closed by the observed remote
  equality, while PR/tag/release/marketplace publication remains active and
  unauthorized. P0B-RES-13 remains closed by C13's descriptor-bound import.
  The three C14-specific non-blocking limitations recorded in P0B-S17 remain
  active and are not silently promoted into product claims.
- **P0B-C15-D04 — next-stage boundary.** Closing P0-B core does not authorize
  G1 source changes. G1 Workbench MVP requires its own bounded stage artifact,
  file allowlist, gates, visual validation matrix and explicit authority
  binding. P0-B hardening may be planned in parallel but must close before P1
  product delivery.

### 4. Steps and objective gates

1. Re-read C00-C14 commit mapping, E17/S17, all residual rows, artifact hashes
   and the real Git remote state, including the separately pushed strategy
   commit that is now C15's exact parent.
2. Append C14 post-push row P0B-E18, self-resolving C15 row P0B-E19, one stage
   closeout row P0B-X01 and one exact four-section P0B-S18 recovery snapshot.
   Record the start/end implementation anchors, completed/planned count, gate
   and review evidence, residual count, unexpected-repair count, workspace
   state and all observable next branches.
3. Obtain an independent exact-diff review of this artifact-only change.
   Critical/Major/Medium must be zero.
4. Run `git diff --check`, `git fsck --no-dangling`, artifact size/hash
   readback, current tracked research-document hash readback, specific
   `v0.6.0` tag/release absence checks and exact one-file allowlist inspection.
   Before staging, `git status --porcelain` must contain exactly the artifact
   as an unstaged modification and `git diff --name-only` must contain exactly
   the artifact, with no staged or unknown path.
   After named staging, `git diff --cached --name-only` must contain exactly
   the artifact, `git diff --cached --check` must pass, and
   `git status --porcelain` must contain exactly the artifact as a staged
   modification with no unstaged or unknown path. Post-commit porcelain must
   be empty and `git diff-tree` must name exactly the artifact. Product tests
   are not rerun because C15 changes no packaged or executable byte; E17/S17
   remain the frozen C14 product evidence.
5. Stage only this artifact, inspect the staged diff, commit exactly as
   `docs(orchestration): close P0-B core delivery`, push non-force, then verify
   exact `HEAD == @{upstream} == remote-tracking == git ls-remote`.

### 5. Execution discipline and capability profile

- Selected profile:
  `native-plan / spawn-send-wait / repo-artifact / native-session-poll`.
  Adapter: Codex desktop on the declared macOS workspace.
- Capability evidence uses exactly the permitted categories:
  - `live capability declarations`: `update_plan`, `spawn_agent`,
    `followup_task`, `send_message`, `wait_agent`, `exec_command`,
    `write_stdin` and `apply_patch` are declared in this session.
  - `observable behavior`: the current session has already updated the native
    plan, dispatched bounded read-only reviews, received their results and run
    commands with observable exit status; native session polling is available
    for any command that yields.
  - `environment identity`: the host exposes Codex desktop, the declared
    macOS workspace and current shell identity passively.
  - `public configuration`: branch and upstream configuration are exposed
    read-only by Git; no additional capability evidence was observed.
- Exact allowlist: only
  `docs/orchestrated/vibecad-p0b-core.md`.
- Circuit breakers are any second C15 diff/staged/commit path, a current
  research hash other than
  `53f75ba475db9b1d3d83e64651a77993b3a6bf5d5a0470ef912193dc33d55deb`,
  unexpected artifact hash, a C15 parent other than
  `7eb4b3a92a937e005509d75b8d6b111b134a9350`, a product end other than
  `157d33f89386499dfbf3d589cd8a57ffffcde434`, a `v0.6.0` tag/release,
  failed diff/fsck, nonzero independent Critical/Major/Medium, push rejection
  or remote inequality. Stop without force, rewrite or opportunistic repair.

### 6. Delivery boundary and residual rules

- C15 closes the P0-B core campaign record; it does not publish 0.6.0 or claim
  host verification, G1 UI, P0-B hardening, P1/P2, enterprise readiness,
  reverse reconstruction, photo/video reconstruction or simulation.
- The final branch retains all active residuals with their original owners and
  observable closure conditions. No residual is fixed inside this docs-only
  packet.
- C14's wheel, sdist, MCPB and standalone Skill in
  `/private/tmp/vibecad-c14-final4.oj3rHb/dist/` are immutable local evidence,
  not a durable release channel.

### 7. Required final report

- Report the exact C15 commit SHA, non-force push result and four-way Git
  equality; one-file diff/stat; review severities; gate results; C00-C15
  completion count; active/closed residual status; the current tracked research
  hash; specific absence of a `v0.6.0` tag/release; the separately pushed
  strategy parent; and the exact next stage.
- If any required observation differs from this packet, preserve the evidence
  and any attempted local C15 commit/state without reset, rewrite or force.
  Do not claim closeout; `7eb4b3a92a937e005509d75b8d6b111b134a9350`
  remains the last three-way-equal branch recovery anchor and C14
  `157d33f89386499dfbf3d589cd8a57ffffcde434` remains the product/package end.

## 33. P0-B Closeout Ledger Addendum

P0B-C15-CORR-01 preserves E17 and S17 as pre-push history and supersedes only
their observable fields. C14 is actually commit
`157d33f89386499dfbf3d589cd8a57ffffcde434`; it was pushed non-force, and
`HEAD`, upstream, remote-tracking and `git ls-remote` were all equal to that
commit before the separate strategy task. S17's C14 commit/push next steps are
complete, its temporary C13 recovery anchor is superseded, and its reference
to the residual ledger as Section 8 is corrected to Section 9. P0B-RES-12's
branch-push portion is closed; its PR/tag/release/marketplace portion remains
active and unauthorized.

| Entry ID | Decision / approval | Commit / push | Gate evidence | Residual | Snapshot | State |
|---|---|---|---|---|---|---|
| P0B-E18 / 2026-07-24T05:25:12Z | P0B-C14 post-push readback under P0B-R1.1/A01/A02; supersedes only E17/S17 observable pre-push fields | C14 `157d33f89386499dfbf3d589cd8a57ffffcde434`, pushed non-force; four-way equality observed before separately pushed strategy commit `7eb4b3a92a937e005509d75b8d6b111b134a9350` | E17 remains canonical product evidence: full non-slow `4902 passed, 108 deselected, 19 warnings`; slow `11 passed, 102 deselected`; managed matrix `2 passed`; MCPB `1 passed`; M05 `1 passed`; reviews `0/0/0/0`; exact receipt/engine/data preservation; four artifacts re-read at their exact sizes/SHA-256; `git fsck --no-dangling` green; local/remote `v0.6.0` tag absent and GitHub `v0.6.0` release absent | P0B-RES-01..11 active; P0B-RES-12 branch-push closed but publication portion active; P0B-RES-13 closed; P0B-RES-14 active; three C14 evidence limitations active | P0B-S18 | completed |
| P0B-E19 / 2026-07-24T05:25:12Z | P0B-C15 under P0B-R1.1/A01/A02 and P0B-C15-D01..D04 | `SELF(P0B-C15)`: unique commit with sole parent `7eb4b3a92a937e005509d75b8d6b111b134a9350`, exact subject `docs(orchestration): close P0-B core delivery`, exact one-file diff, and tree containing E18/E19/X01/S18; completion requires authorized non-force push and four-way equality to SELF | corrected readback after preserved zsh `path` setup error; exact C00-C14 mapping; strategy-parent four-way equality; one-file pre-stage gate; diff/fsck; exact artifact/research hashes; specific `v0.6.0` tag/release absence; packet review and two independent final-addendum reviews all GO `Critical 0 / Major 0 / Medium 0 / Minor 0`; staged checks, SELF checks and post-push equality remain required by the predicate | 13 active residual IDs: P0B-RES-01..12 plus P0B-RES-14, with RES-12 partial; P0B-RES-13 closed; three C14 evidence limitations active | P0B-S18 | completed iff SELF predicate passes; otherwise blocked with attempted state preserved |

| Closeout ID | Stage span | Completion / budget | Gate and review summary | Residual summary | Final snapshot | State |
|---|---|---|---|---|---|---|
| P0B-X01 | Stage-3 predecessor `4d8dc88017f658c93cd97c8ee616b9905c3af781`; first P0-B commit C00 `6eb209d99028520113c953d3eb4c8f42d43bae1c`; product/package end C14 `157d33f89386499dfbf3d589cd8a57ffffcde434`; orchestration end `SELF(P0B-C15)` after separately pushed non-P0-B strategy parent `7eb4b3a92a937e005509d75b8d6b111b134a9350` | 16/16 planned packet IDs terminal when SELF predicate passes; P0-B commits 17/20 including A02 amendment `a7e6881e21936dc5b5dc2c92f5c1dd70b9498dfe`; 3 hard-budget slots remain; additional repair commits used 0; branch has 18 post-Stage-3 commits including the interleaved strategy commit | P0B-E04..E19 evidence retained; final C14 counts/hashes/real FreeCAD gates pass; C15 artifact/hash/Git readback and one-file checks pass; package/runtime/semantic reviews for C14 plus packet and final-addendum reviews for C15 are zero-finding; staged/commit/push predicates remain mandatory | 13 active residual IDs, 1 closed ID (RES-13), plus 3 active C14 non-ID evidence limitations; no residual silently fixed or dropped | P0B-S18 | completed iff SELF predicate passes; otherwise blocked |

## 34. Recovery Snapshot P0B-S18

### 1. Completed milestones

- Repository is `/Users/wangtao/Documents/DevProject/vibecad`, branch
  `codex/agent-stage3`, artifact revision P0B-R1.1 plus the append-only D21 and
  C15 addenda. Stage 3 predecessor is
  `4d8dc88017f658c93cd97c8ee616b9905c3af781`.
- The exact P0-B packet map is:

  ```text
  C00 6eb209d99028520113c953d3eb4c8f42d43bae1c
  A02 a7e6881e21936dc5b5dc2c92f5c1dd70b9498dfe
  C01 aeac5c5d42149e2b9030e8d11cda0380122be6c7
  C02 5ecb77a9f9d87bd624dd6a4a38685c3b75711718
  C03 29cf532a83b33f28935dfe8fbcaa7f437309e1f5
  C04 8c07ade51f8f6c889bda46290375ad96e981618e
  C05 65b7e9a35a7cb80317f56c0b4b92553716807561
  C06 72817966f08aeda7fc93f24fe3e4e0c70f8712ed
  C07 d004992d6b8fee25439b720f1e8be3ff47558f88
  C08 2a77b860874e04d4cdae07fd54bcb6c0e03e5e6a
  C09 11ff62968db6945061767c6fee2ed902f1ad569e
  C10 23339a70d59a803fd7245312be410dfb23f0a084
  C11 6ee3f9b2da60134da6906b3ab9ba6d8d17fb57f3
  C12 a5fba84b8c50980f73051de563445c1138fad7b3
  C13 cf87fba0308f9a32820bf5237af61ea4e2d32989
  C14 157d33f89386499dfbf3d589cd8a57ffffcde434
  C15 SELF(P0B-C15)
  ```

- C00-C14 are committed and pushed. C14 closes the product/package work with
  version 0.6.0, runtime epoch 4, FreeCAD 1.1.0, MCP 1.27.2, exactly 28 public
  tools and canonical public SHA-256
  `ae495ba457af40a5837a03e77eef4b396b0a4209755878350bc341ac7de8bfd3`.
  Its final gates remain E17's `4902` full, `11` slow, `2` managed public,
  `1` MCPB and `1` M05, with independent final review
  `Critical 0 / Major 0 / Medium 0 / Minor 0`.
  C15's packet and complete addendum likewise received independent final
  reviews with `Critical 0 / Major 0 / Medium 0 / Minor 0`.
- The four immutable local candidate records re-read exactly:
  - wheel: 599,337 bytes,
    `3c73451aa6fd209e7e4877abad6fba0200ff97a8f6bbca45c5e4a4d5ab31014d`;
  - sdist: 639,203 bytes,
    `4fc514cd49815e92c213686fcbdfe0847e651a2502baf8d68f264b4fc6e1aa83`;
  - MCPB: 703,655 bytes,
    `1eb2f468cc9995da330cc8e6511a40e68eae04be90657e1f8f00c0beb8b9b1cc`;
  - Skill: 4,116 bytes,
    `db27e09408a0fbe8e3a275c53bf88ffad1dd60c1adf7dfe36e03ca8f9622de28`.
- A separate user-owned strategy task completed and was pushed as
  `7eb4b3a92a937e005509d75b8d6b111b134a9350`. It has sole parent C14, exact
  subject `docs(strategy): consolidate product and backend direction`, changes
  exactly five strategy/research paths and does not include this orchestration
  artifact. It is outside the P0-B packet/budget count but is the exact parent
  of C15. The research document is now tracked at SHA-256
  `53f75ba475db9b1d3d83e64651a77993b3a6bf5d5a0470ef912193dc33d55deb`.
- P0-B is 16/16 packets, 17/20 P0-B commits and 0 additional repair commits
  only when `SELF(P0B-C15)` exists and its push/equality predicate passes. The
  branch then contains 18 commits after the Stage 3 predecessor. S18 is the
  final closeout snapshot only under that predicate; otherwise it is a
  preserved blocked-attempt snapshot and `7eb4b3a...` remains the last
  three-way-equal branch recovery anchor.

### 2. Next steps

1. Pre-commit branch: require four-way equality at
   `7eb4b3a92a937e005509d75b8d6b111b134a9350`; pre-stage porcelain must show
   only this artifact as unstaged; stage exactly this artifact; cached
   name/check must be exact and post-stage porcelain must show only this
   artifact as staged.
2. Post-commit/pre-push branch: require `HEAD=SELF(P0B-C15)`, sole parent
   `7eb4b3a...`, exact subject, exact one-file diff and E18/E19/X01/S18 tree
   content; porcelain must be empty while upstream, remote-tracking and
   `git ls-remote` still equal `7eb4b3a...`.
3. Push `SELF(P0B-C15)` non-force. If all four refs then equal SELF, P0-B core
   is formally closed. Resolve and report the actual SELF SHA from Git.
4. If staging, commit, push or equality differs, preserve every local attempt
   and status without reset, rewrite or force; do not claim closeout and do not
   start G1. Recover from the last three-way-equal branch anchor while keeping
   C14 as the product/package end.
5. After successful closeout, the next packet is an independent G1 FreeCAD Qt
   Workbench MVP plan and authority binding. P0-B hardening may be planned in
   parallel but retention/GC, runner migration and observability/recovery must
   close before P1 delivery. G1 implementation does not inherit C15 authority.

### 3. Approved decisions

- P0B-A01 approves P0B-R1, P0B-D01..D22 plus D08A/D17A, C00-C15,
  allowlists, gates, budgets and exclusions. P0B-A02/P0B-D22-R1 approves only
  immediate non-force pushes to the current branch; it does not authorize PR,
  tag, release, marketplace publication, force-push or external spend.
- P0B-C15-D01..D04 govern self-resolving identity, completion accounting,
  residual disposition and the next-stage boundary. The interleaved strategy
  commit is separately owned and does not expand P0-B or C15 scope.
- Product invariants remain: expert CAD Agent; user-owned host model/token;
  one authenticated daemon/Application/Task Kernel for domain calls; bounded
  ModelProgram rather than arbitrary Python; Workbench as a client rather than
  a second authority; external reconstruction/simulation through providers.
- Real Claude/Codex host activation, G1 UI and P0-B hardening remain residuals.
  No product-level direction is silently inferred from closeout.

### 4. Execution discipline

- Capability profile:
  `native-plan / spawn-send-wait / repo-artifact / native-session-poll`;
  adapter: Codex desktop on the declared macOS workspace.
- Capability evidence remains:
  - `live capability declarations`: native plan, collaboration delegation,
    command/session polling and patch tools are declared;
  - `observable behavior`: bounded independent reviews and commands have
    returned observable results in this session;
  - `environment identity`: Codex desktop and the declared macOS workspace are
    exposed passively;
  - `public configuration`: Git exposes branch/upstream read-only; no further
    evidence observed.
- Exact C15 allowlist is only
  `docs/orchestrated/vibecad-p0b-core.md`. Current research is tracked,
  outside the diff, and must retain SHA-256
  `53f75ba475db9b1d3d83e64651a77993b3a6bf5d5a0470ef912193dc33d55deb`.
- Required gates are exact pre/post-stage porcelain, unstaged/cached
  name lists, unstaged/cached diff checks, `git fsck --no-dangling`, four
  artifact size/hash readback, research hash readback, specific absence of a
  local/remote `v0.6.0` tag and GitHub `v0.6.0` release, independent exact-diff
  review, SELF parent/subject/path/tree checks, non-force push and four-way
  equality. Product tests remain frozen at E17 because C15 changes no
  executable or packaged byte.
- Circuit breakers are any second path or unknown porcelain item, artifact or
  research mismatch, wrong C15 parent/subject/tree, changed C14 product end,
  `v0.6.0` publication, diff/fsck/review RED, push rejection or unequal refs.
  Preserve evidence; never reset, rewrite, force, hide or repair around one.
- Residual rules: 13 IDs remain active (P0B-RES-01..12 and P0B-RES-14, with
  RES-12's branch portion closed), P0B-RES-13 is closed, and the three
  C14-specific evidence limitations remain active. The authoritative residual
  ledger is Section 9.
- Ordered recovery checks are: inspect exact porcelain and branch; resolve
  HEAD/upstream/remote-tracking/`git ls-remote`; validate SELF sole parent,
  subject, one-file diff and E18/E19/X01/S18 content; re-read artifact/research
  hashes and `v0.6.0` absence; run diff checks and fsck; then follow the
  observable branch in Section 2. A successful final workspace is tracked
  clean with no staged, unstaged or unknown path.
