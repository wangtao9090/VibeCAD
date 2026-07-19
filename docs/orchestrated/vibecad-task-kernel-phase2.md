# VibeCAD Deterministic Task Kernel Phase 2 Orchestrated Campaign

- Campaign: vibecad-task-kernel-phase2
- Active revision: TK-R1
- Status: TK-R1 approved / Stage C executing
- Prepared: 2026-07-16
- Repository anchor: codex/agent-core-phase1@bf8967077addfbd8002d6289b0eb925f3f55b638
- Anchor upstream: origin/codex/agent-core-phase1@bf8967077addfbd8002d6289b0eb925f3f55b638
- Target branch after approval: codex/task-kernel-phase2
- Push policy: push the target branch after each independently accepted commit; never force-push and never push directly to main
- Pull-request policy: not authorized by this revision

This file is the authoritative scope, approval, evidence, recovery, and handoff
record for the deterministic Task Kernel campaign. It is the only repository
write authorized before TK-R1 approval. After approval, its execution ledger is
append-only.

The user's 2026-07-16 instruction “开始吧” authorizes preparation of this
revision. It preceded the concrete Stage C scope below and is therefore not
treated as approval to change production code. The exact approval phrase is
recorded at the end of this document.

## Capability Profile

    approval: native-plan
    delegation: spawn-send-wait
    persistence: repo-artifact
    process: native-session-poll

### Adapter evidence record

- live capability declarations
  - Native plan projection is available through update_plan.
  - Delegation supports spawn_agent, send_message, followup_task, and
    wait_agent.
  - Worker creation declares model and reasoning-effort selectors.
  - Repository edits are available through apply_patch.
  - Long-running commands can return a live session and be polled through
    write_stdin.
  - No native durable-memory interface is declared.
- observable behavior
  - Native plan projection has accepted plan state in this task.
  - Spawn/send/wait collaboration returned three bounded, read-only Stage C
    design reviews.
  - Repository artifact persistence, exact staging, commit, push, and native
    command polling were exercised successfully in Phase 1.
- environment identity
  - Host: Codex desktop.
  - Controller: /root.
  - Workspace: /Users/wangtao/Documents/DevProject/vibecad.
  - Current branch and clean anchor:
    codex/agent-core-phase1@bf8967077addfbd8002d6289b0eb925f3f55b638.
- public configuration
  - Filesystem permission profile is unrestricted for the declared workspace.
  - Network access is enabled.
  - Approval policy is never; no escalation path is available or required.
  - Workspace roots are the VibeCAD repository and the declared Codex
    visualization workspace.

The repository artifact remains authoritative. Native plan state is only a
projection. Explicit user approval remains mandatory before implementation.

### Role and model routing

| Role | Required tier | Planned use |
|---|---|---|
| controller | inherited | scope, acceptance, ledger, exact staging, commit, push |
| implementation worker | standard | one bounded commit packet at a time |
| independent reviewer | deep | read-only adversarial review after GREEN |
| test worker | standard | focused, compatibility, full, and real-FreeCAD evidence |

When an explicit worker selector is used, the standard implementation mapping
is gpt-5.6-terra/high and the deep review mapping is gpt-5.6-sol/high. Every
packet must record the actual selector or state that it inherited the
controller model; no unobserved routing will be claimed.

## Stage C — Deterministic Task Kernel

### 1. Context

Phase 1 established versioned workflow contracts, the operation registry,
ModelProgram validation, tool-result normalization, and an in-process execution
adapter. It deliberately did not own TaskRun persistence, project locking,
candidate revisions, independent AcceptanceSpec verification, or commit and
rollback.

Stage C implements architecture Phase 2 without adding a model backend or
public MCP task tools. Given an already-written ModelProgram, the internal
kernel will validate it, execute it in an isolated candidate FreeCAD Session,
collect trusted geometry and artifact observations, verify its AcceptanceSpec,
and atomically commit an immutable revision or discard the candidate.

The acceptance gate is:

1. A real FreeCAD flow creates a Box, modifies a dimension, independently
   verifies bbox, volume, validity, and solid count, exports STEP and FCStd, and
   advances the committed project revision.
2. A later candidate that first mutates successfully and then encounters an
   injected operation failure rolls back without changing committed HEAD,
   committed FCStd hash, or the live committed Session binding.
3. A fully executed candidate with a deliberately failing required acceptance
   criterion also cannot commit.
4. Every outcome leaves a strict, reloadable, diagnostic TaskRun.

### 2. Decisions

- TK-D01 — Phase 2 supports reasoning_owner=external_plan only. The state
  contract can represent mcp_sampling and byok, but TaskService rejects those
  modes. There is no model SDK, model key, network call, token accounting, or
  nested reasoning in this campaign.
- TK-D02 — TaskRun is a strict, versioned, immutable JSON contract. It stores
  the submitted ModelProgram, StepResults, transition records, verification
  reports, revisions, artifact references, last error, and derived next action.
  It never persists ValidatedProgram or a callable.
- TK-D03 — On submit/continue, ModelProgram and AcceptanceSpec byte budgets,
  program validation, acceptance compilation, task/program identity checks,
  and base-revision identifier checks occur before candidate/project mutation
  or FreeCAD is touched. create_task may read the current project HEAD to bind
  its base revision. Reading and updating the TaskRun store is allowed because
  it is the control-plane record, not a CAD side effect.
- TK-D04 — Task-store CAS generation is persistence metadata in a StoredTaskRun
  envelope. TaskRun transition sequence is separate audit metadata. Neither is
  inferred from timestamps.
- TK-D05 — All externally supplied identifiers use canonical forms such as
  task_<32-lowercase-hex> and project_<32-lowercase-hex>. Filesystem paths use a
  SHA-256 digest of those identifiers. ModelProgram content can never select a
  task, revision, artifact, lock, or export pathname.
- TK-D06 — Project writes require a ResourceLease backed by an in-process lock
  plus an operating-system advisory lock. The file descriptor is authoritative;
  PID/time text is diagnostic only. There is no TTL-based lock stealing or
  stale-lock deletion. Process exit releases the OS lease.
- TK-D07 — Only an explicitly configured, trusted local storage root with
  ordinary-file, atomic rename, file fsync, and directory fsync semantics is in
  scope. The implementation makes no durability claim for a network filesystem;
  an unknown root or any observed unsupported/uncertain durability result is
  rejected rather than silently weakened.
- TK-D08 — A project is an immutable revision set plus an atomically replaced
  HEAD manifest. A committed revision contains model.FCStd, trusted exported
  artifacts, hashes, sizes, and a manifest. A commit journal records intent and
  result around the HEAD linearization point.
- TK-D09 — Agent revision commit is distinct from the existing user-facing
  save_project dirty-state contract. Stage C does not overwrite an arbitrary
  user project path and does not call Session.mark_saved to manufacture user
  save semantics.
- TK-D10 — Every candidate uses a new Session loaded from the immutable base
  artifact, or an empty new-project base. It never executes against the current
  committed Session. Rollback closes/discards only the candidate; it never
  reloads or rewrites the baseline.
- TK-D11 — Candidate revision IDs are generated and owned by the revision
  coordinator. They are not Session._revision_id, a document name, a user path,
  a base-revision derivative, or a value invented by the ModelProgram.
- TK-D12 — After execution, the candidate is checkpointed to private staging
  and reloaded. The STEP export runs once against that checkpoint-reloaded,
  read-only Session because sealing requires both artifacts to exist. The
  revision is then sealed and reloaded from immutable FCStd; only trusted
  read-only geometry and artifact observation runs after sealing. The final
  manifest hashes the model and STEP artifact.
- TK-D13 — Independent acceptance evidence comes from a trusted
  ObservationSnapshot. StepResult values and C5 execution_acknowledged evidence
  cannot satisfy a criterion.
- TK-D14 — Acceptance compilation is fail-closed. An empty spec, duplicate
  criterion ID, pure visual spec, unknown required check, missing fact,
  ambiguous target, non-finite number, implicit unit conversion, or missing
  baseline for preservation cannot authorize commit.
- TK-D15 — The initial deterministic check allowlist is:
  geometry volume, area, bbox, and center_of_mass; topology valid_shape and
  solid_count; artifact exists, non_empty, and format. Preservation, assembly,
  feature inference, and visual checks remain unsupported in TK-R1.
- TK-D16 — Artifact paths are coordinator-owned. Stage C materializes one STEP
  artifact and the committed FCStd under candidate staging. The model cannot
  request arbitrary output directories. STL, glTF, split export, and configurable
  export sets are not added to ModelProgram.
- TK-D17 — A successful VerificationReceipt is an authentic internal capability
  bound to candidate revision, manifest SHA-256, observation digest, and the
  compiled AcceptanceSpec. Candidate commit rejects absent, forged, replayed,
  mismatched, or failed receipts.
- TK-D18 — The HEAD replacement is the project commit linearization point.
  Failure before it leaves the old revision committed. Once HEAD advances, a
  later Session cleanup or TaskRun write failure cannot roll HEAD backward;
  journal reconciliation completes or reports recovery_required.
- TK-D19 — SessionSlot promotion uses compare-and-swap while the project lease
  is held. A mismatch cannot silently publish a different live Session.
  Server-global Session integration is deferred, so the Stage C slot is injected
  and tested internally.
- TK-D20 — TaskService persists each semantic transition with CAS. A candidate
  failure must enter rolling_back before failed or needs_input. A pre-candidate
  base conflict can enter needs_input directly. Terminal states have no
  successors.
- TK-D21 — Stage C performs zero automatic step retries and zero automatic
  repair/replan loops. Pre-candidate validation, lease, or base conflicts may
  return needs_input. After a candidate revision is durably published, every
  execution, geometry, runtime, policy, cancellation, and required-verification
  failure rolls back to failed; a revised plan starts a new TaskRun under
  TK-D33. R-B16 prevents broader tool-specific retry policy until stable
  semantic tool error codes and multi-attempt lineage exist.
- TK-D22 — Existing low-level MCP tools and server.py remain unchanged. The new
  TaskService is internal and must not be publicly exposed while those write
  paths can bypass its project lease.
- TK-D23 — Explicit injected handlers remain a trusted same-process application
  boundary. No dynamic import, entry-point discovery, source execution,
  arbitrary Python, shell, or plugin callable is accepted from a ModelProgram.
- TK-D24 — Every semantic commit follows genuine RED, bounded GREEN, cumulative
  regression, independent read-only review, named-file staging, commit, and
  immediate non-force push. Any architecture or allowlist change requires a new
  artifact revision and user approval.

### 3. Commit Sequence

The commit budget is nine. The campaign artifact is appended with evidence and
is a named file in every commit. No tenth self-referential ledger commit is
planned.

#### TK1 — feat(workflow): define durable task-run state contracts

Named files:

- src/vibecad/workflow/state.py
- tests/test_task_state.py
- docs/orchestrated/vibecad-task-kernel-phase2.md

Deliver:

- ReasoningOwner, TaskStatus, TaskEvent, NextAction, TaskArtifactRef,
  CriterionVerdict, VerificationReport, TaskTransitionRecord, TaskStepRecord,
  TaskRun, new_task_run, transition_task, append_step_result,
  append_verification, and next_action_for.
- Strict schema-v1 mapping round trips, canonical IDs, cross-field invariants,
  terminal-state protection, sequence budgets, and full transition matrix.
- program.task_id/task.id and program.base_revision/task.base_revision equality.
- external_plan-only service policy represented without importing a model,
  filesystem, MCP, CAD, or validation engine.

Gates:

1. Genuine missing-module RED in tests/test_task_state.py.
2. Exhaustive status/event matrix, malformed mapping, budget, mutation,
   invariant, and round-trip GREEN.
3. C1-C5 compatibility, pure-import, Ruff, format, allowlist, and diff gates.
4. Independent reviewer returns ACCEPT with P0=0 and P1=0.

#### TK2 — feat(workflow): coordinate exclusive resource leases

Named files:

- src/vibecad/workflow/lease.py
- tests/test_workflow_lease.py
- docs/orchestrated/vibecad-task-kernel-phase2.md

Deliver:

- ResourceLeaseManager, ResourceLease, ProjectWriteLease, stable error codes,
  hashed resource keys, owner tokens, exact release, and context management.
- In-process thread exclusion plus POSIX fcntl and Windows msvcrt adapter
  branches. No fixed-time reclaim.

Gates:

1. Genuine RED for the absent lease module.
2. Two-instance, multi-thread, subprocess contention, wrong-owner release,
   process-exit release, symlink/non-regular lock, and different-resource tests.
3. Import, compatibility, lint, format, diff, and independent review gates.
4. If Windows semantics cannot be run objectively on this host, keep Windows
   unclaimed and record TK-R07; never fall back to an unlocked implementation.

#### TK3 — feat(workflow): persist task runs atomically

Named files:

- src/vibecad/workflow/store.py
- tests/test_task_store.py
- docs/orchestrated/vibecad-task-kernel-phase2.md

Deliver:

- StoredTaskRun and TaskRunStore create/load/compare_and_set.
- Canonical deterministic JSON, duplicate-key and NaN/Infinity rejection,
  payload checksum, two-MiB record cap, hashed task paths, private permissions,
  ordinary-file checks, same-directory exclusive temporary files, file fsync,
  os.replace, directory fsync, and cleanup.
- Stable invalid_id, not_found, already_exists, conflict, corrupt_record,
  record_too_large, unsafe_store, lock_unavailable, io_error, and
  durability_uncertain failures.

Gates:

1. Genuine RED for the absent store.
2. Fault injection at write, flush, file fsync, replace, and directory fsync;
   readers can observe only the complete old or complete new record.
3. CAS races, concurrent process writes, traversal/Unicode/separator IDs,
   symlink/non-regular/hardlink boundaries, checksum mismatch, truncation,
   duplicate key, oversized record, and temp-cleanup tests.
4. Cumulative TK1-TK3, C1-C5, import, lint, format, diff, and independent review.

#### TK4 — feat(validation): verify deterministic acceptance criteria

Named files:

- src/vibecad/validation/__init__.py
- src/vibecad/validation/contracts.py
- src/vibecad/validation/checks.py
- src/vibecad/validation/engine.py
- tests/test_acceptance_verifier.py
- docs/orchestrated/vibecad-task-kernel-phase2.md

Deliver:

- Strict ShapeObservation, ArtifactObservation, ObservationSnapshot, compiled
  acceptance capability, and authentic receipt. The engine returns the durable
  criterion verdicts and VerificationReport defined by TK1.
- compile_acceptance_spec and verify_acceptance with the TK-D15 closed check
  allowlist.
- Required/optional semantics, explicit canonical units, scalar/vector
  tolerances, deterministic target lookup, observation and manifest binding,
  and fail-closed unsupported behavior.

Gates:

1. Genuine RED for absent validation modules.
2. Per-check pass/fail/boundary cases; exact tolerance boundary; bool-as-number,
   non-finite, wrong unit/vector, duplicate/missing target, revision/digest
   mismatch, empty/pure-visual spec, optional unsupported, and receipt forgery.
3. Prove StepResult and execution_acknowledged cannot satisfy acceptance.
4. Prove validation imports and executes without filesystem, network, FreeCAD,
   MCP, model SDK, or dynamic callable lookup.
5. Cumulative Stage C, C1-C5, lint, format, diff, and independent review.

#### TK5 — feat(execution): persist immutable CAD revisions

Named files:

- src/vibecad/execution/revisions.py
- tests/test_revision_store.py
- docs/orchestrated/vibecad-task-kernel-phase2.md

Deliver:

- RevisionRef, RevisionArtifactRef, ProjectHead, CommitJournal, and
  LocalRevisionStore.
- New empty-project initialization and trusted-host FCStd import.
- Private project/revision/candidate/transaction layout, immutable manifests,
  model/artifact SHA-256 and sizes, atomic HEAD, and journal reconciliation.
- All user-visible IDs remain opaque; trusted host import paths never enter
  ModelProgram.

Gates:

1. Genuine RED for the absent revision store.
2. Missing/empty/hash-mismatched/directory/symlink artifacts; ID and path
   attacks; permission, temp, replace, fsync, HEAD, manifest, and journal fault
   injection.
3. Crash-window matrix: staging only; complete orphan revision before HEAD;
   HEAD advanced before committed journal; corrupt HEAD/manifest/hash.
4. Prove old revisions remain byte-identical and recovery never guesses.
5. Cumulative, C1-C5, pure-import, lint, format, diff, and independent review.

#### TK6 — feat(execution): isolate candidate sessions

Named files:

- src/vibecad/execution/candidate.py
- tests/test_candidate_revision.py
- docs/orchestrated/vibecad-task-kernel-phase2.md

Deliver:

- CadSnapshotPort, SessionBinding, SessionSlot, ActiveCandidate,
  CheckpointedCandidate, SealedCandidate, CandidateCoordinator, commit,
  rollback, and reconcile results.
- Lease validation, HEAD/base recheck, independent Session creation, sealed
  FCStd reload, receipt verification, atomic HEAD commit, SessionSlot CAS, and
  exact-once terminal behavior.
- No access to Session._revision_id, _saved_revision_id, current
  Session._replace_document, or runtime installer FileLock.

Gates:

1. Genuine RED for the absent candidate coordinator.
2. Fake-session proof that base and candidate are distinct and rollback leaves
   base object/binding untouched.
3. Wrong/replayed/mismatched receipt, seal-after-write, changing HEAD,
   save/close/CAS exceptions, duplicate commit/rollback, cleanup_required, and
   recovery_required tests.
4. HEAD advancement remains committed after post-linearization cleanup failure.
5. Cumulative, C1-C5, pure-import, lint, format, diff, and independent review.

#### TK7 — feat(execution): collect trusted CAD verification evidence

Named files:

- src/vibecad/execution/executor.py
- tests/test_program_executor.py
- docs/orchestrated/vibecad-task-kernel-phase2.md

Deliver:

- InProcessCadExecutor that reuses validate_model_program and the C5
  execute_validated_program adapter.
- Explicit internal bindings for the existing default operations:
  create_document/new_document, create_box/add_box,
  modify_parameter/modify_part, and inspect_model/describe_part.
- Session checkpoint/load/close port, trusted geometry observation, controlled
  STEP export, FCStd artifact metadata, exception redaction, and zero retries.
- No server-global Session, dynamic handler discovery, model-supplied path, or
  new registry operation.

Gates:

1. Genuine RED for the absent executor.
2. Handler preflight, ordered exact-once execution, stop-on-first-failure,
   observation ownership, export path confinement, format detection, artifact
   hashing, mutation-after-input, and exception tests with fake sessions.
3. A bounded opt-in real-FreeCAD smoke proves independent baseline/candidate
   documents and sealed reload if the new adapter first requires real CAD.
4. Cumulative, C1-C5, import, lint, format, diff, and independent review.

#### TK8 — feat(workflow): execute task runs transactionally

Named files:

- src/vibecad/workflow/state.py
- src/vibecad/workflow/service.py
- tests/test_task_state.py
- tests/test_task_service.py
- docs/orchestrated/vibecad-task-kernel-phase2.md

Deliver:

- Internal TaskService.create_task, submit_model_program, continue_task,
  get_task, and reconcile_task.
- Exact order:
  request budget and contract checks; acceptance compile; program validation;
  TaskRun CAS; project lease; HEAD/base recheck; candidate begin; execution;
  checkpoint/reload; controlled export; seal/immutable reload; trusted
  observation; verification; committing record; candidate commit or rollback;
  final TaskRun CAS.
- Policy for needs_input, failed, durability_uncertain, cleanup_required, and
  recovery_required. No automatic repairing state is entered in TK-R1.

Gates:

1. Genuine RED for the absent service.
2. Fake-port coverage of every state and failure edge, including proof that
   rejection before candidate creation performs no project/CAD call.
3. Store-CAS conflict and project-writer contention; recovery after each
   commit window; every failure after candidate publication traverses
   rolling_back, while an unpublished begin/CAS prepare abort performs no
   ModelProgram command and is recorded as a pre-candidate conflict.
4. Invalid/failed/forged verification can never invoke HEAD commit.
5. Cumulative Stage C, C1-C5, full normal suite, lint, format, diff, and
   independent review.

#### TK9 — test(workflow): prove candidate commit and rollback with FreeCAD

Named files:

- tests/test_task_kernel_integration.py
- docs/AGENT_ARCHITECTURE.md
- docs/orchestrated/vibecad-task-kernel-phase2.md

Deliver:

- Real FreeCAD create → modify → inspect → checkpoint → export → verify →
  commit proof with bbox [12, 20, 30], volume 7200 mm3, one valid solid, nonempty
  STEP, nonempty FCStd, matching hashes, committed HEAD, and reloadable geometry.
- Real execution-failure proof after an earlier candidate mutation.
- Real required-verification-failure proof after successful execution.
- Both failure cases prove unchanged committed HEAD/model hash and diagnostic
  TaskRun; rollback leaves the committed Session usable.
- Update the accepted architecture state/recovery description, initial verifier
  matrix, immutable revision semantics, and remaining Stage 3 integration
  boundary. Do not claim MCP exposure.

Gates:

1. Genuine RED before the integration fixture exists.
2. Full normal test suite.
3. Real FreeCAD gate using only the already-installed environment:

       VIBECAD_RUN_INTEGRATION=1
       VIBECAD_FREECAD_ENV="/Users/wangtao/Library/Application Support/VibeCAD/mamba/envs/vibecad"
       PYTHONPATH=src .venv/bin/pytest -q -m slow tests/test_task_kernel_integration.py

4. Installer fallback, downloads, environment creation, and upgrades are
   forbidden during this gate.
5. Manually inspect revision layout, HEAD, journal, manifest, file sizes, and
   hashes; confirm all runtime data is confined to the test temp root.
6. Final C1-C5 compatibility, full normal suite, Ruff lint/format, diff,
   forbidden-import scan, independent deep review, exact staging, commit, push,
   clean tree, and HEAD/upstream equality.

### 4. Manual Validation Matrix

| Check | Owner | Mode | Required evidence |
|---|---|---|---|
| State transition table and derived next actions | controller | readback + exhaustive tests | all allowed and forbidden edges accounted for |
| Same-project writer contention | test worker | thread + subprocess | exactly one lease; process exit releases it |
| Atomic TaskRun replacement | test worker | fault injection | complete old or complete new bytes only |
| Revision HEAD crash windows | reviewer | artifact/journal readback | deterministic reconcile result for every window |
| Candidate isolation | reviewer | fake + real FreeCAD identity/state comparison | baseline geometry, dirty, roots, undo/redo, and binding unchanged on rollback |
| Acceptance proof provenance | reviewer | adversarial unit review | only trusted snapshot; receipt bound to manifest digest |
| Controlled exports | controller | real artifact inspection | STEP/FCStd nonempty, detected format and hashes match manifest |
| Successful real flow | test worker | installed FreeCAD | create/modify/verify/export/commit and clean reload |
| Execution failure | test worker | installed FreeCAD | prior candidate mutation discarded; committed hash unchanged |
| Verification failure | test worker | installed FreeCAD | completed execution cannot bypass failed required criterion |
| Existing behavior | test worker | automated | C1-C5 compatibility and full normal suite pass |
| Final scope | controller | git diff/status/readback | only allowlisted files; clean branch equals upstream |

No user-present GUI validation is required. If a required fact cannot be
observed headlessly, it becomes a blocker rather than an invented pass.

### 5. Budget and Circuit Breakers

- Commit budget: exactly 9 planned commits.
- Repair budget: at most 2 bounded GREEN attempts per commit. A third failure
  requires a new artifact revision.
- Review budget: one independent read-only review after GREEN per semantic
  commit; any P0 or P1 blocks acceptance. P2 requires closure or explicit
  residual disposition before staging.
- Program budget: existing maximum 64 commands; no automatic command retry.
- Task budget: at most 64 StepResults, 128 transition records, 16 verification
  reports, 128 artifact references, and 2 MiB canonical stored record.
- Reasoning/repair budget: zero model calls, zero replans, zero automatic repair
  loops, and zero nested tools.
- Lock budget: nonblocking or bounded acquisition, never more than 5 seconds in
  tests or the synchronous service path.
- Real-CAD budget: use the installed FreeCAD 1.1.0 / Python 3.12.13 environment;
  no install, download, upgrade, or alternate environment.
- Push budget: one non-force push after each accepted commit; no main push, tag,
  release, PR, remote rewrite, or force operation.

Stop immediately and issue a new revision if:

- a file outside the allowlist must change;
- server.py, engine/session.py, an existing tool, registry, program validator,
  adapter, normalizer, dependency, runtime, manifest, or release workflow must
  change;
- a secret, model key, credential, arbitrary code path, dynamic import, shell,
  network call, or model-supplied filesystem path appears;
- schema/policy/acceptance rejection touches project storage or FreeCAD;
- two writers can hold the same project lease;
- candidate execution touches the committed Session;
- failed execution or failed required acceptance can advance HEAD;
- a receipt is not bound to the exact sealed manifest;
- HEAD, journal, or TaskRun reconciliation is ambiguous;
- file or directory fsync reports uncertain durability and the code attempts to
  repeat CAD side effects automatically;
- the baseline cannot remain byte-identical through a rollback test;
- the real FreeCAD gate would require installer fallback;
- a required gate cannot pass within the declared scope and attempt budget.
- the configured persistence root cannot be established as a trusted local
  storage root with the declared durability contract.

### 6. Exact File Allowlist

Only these paths may change after approval:

- docs/orchestrated/vibecad-task-kernel-phase2.md
- docs/AGENT_ARCHITECTURE.md
- src/vibecad/workflow/state.py
- src/vibecad/workflow/lease.py
- src/vibecad/workflow/store.py
- src/vibecad/workflow/service.py
- src/vibecad/execution/revisions.py
- src/vibecad/execution/candidate.py
- src/vibecad/execution/executor.py
- src/vibecad/validation/__init__.py
- src/vibecad/validation/contracts.py
- src/vibecad/validation/checks.py
- src/vibecad/validation/engine.py
- tests/test_task_state.py
- tests/test_workflow_lease.py
- tests/test_task_store.py
- tests/test_acceptance_verifier.py
- tests/test_revision_store.py
- tests/test_candidate_revision.py
- tests/test_program_executor.py
- tests/test_task_service.py
- tests/test_task_kernel_integration.py

Explicitly excluded:

- src/vibecad/server.py and all public MCP registration.
- src/vibecad/engine/session.py and its private checkpoint/revision fields.
- Existing src/vibecad/tools, feedback, runtime, registry.py, adapter.py,
  results.py, workflow/contracts.py, workflow/program.py, and workflow/errors.py.
- pyproject.toml, uv.lock, manifest.json, mcpb_entry.py, release automation, and
  dependency changes.
- Model, reasoning, provider, photo/video, mesh/STL-to-CAD, simulation, arbitrary
  Python, desktop, CLI, HTTP, and GUI implementation.

An allowlisted path is permission, not a requirement. Unneeded files must not
be touched.

### 7. Expected Impact

| Area | Expected change |
|---|---|
| Existing MCP users | none; current low-level tools and server behavior remain unchanged |
| Model/provider behavior | none; no model is called and no key is accepted |
| CAD safety | programs execute only in an isolated candidate Session |
| Persistence | strict TaskRun JSON plus immutable local revision artifacts and atomic HEAD |
| Concurrency | one active writer per project in the supported local process/filesystem model |
| Verification | deterministic allowlisted geometry/topology/artifact checks own commit authority |
| Export | internal task-owned STEP and FCStd artifacts only |
| Recovery | journal/HEAD reconciliation distinguishes not-committed, committed, cleanup-required, and recovery-required states |
| Performance | one extra FCStd checkpoint/reload and hashing pass per candidate; two live documents may temporarily coexist |
| Public API | no new MCP tool yet; Stage 3 can wrap the internal TaskService |

The expected source change is additive and internal. Pure state, store, lease,
revision, and validation imports must not initialize FreeCAD, MCP, a model SDK,
or the network.

### 8. Residuals and Closure Conditions

| ID | Residual | Disposition / owner | Closure condition |
|---|---|---|---|
| TK-R01 | No public create_task/submit/continue/get MCP tools | intentional; Stage 3 | separately approved external-Agent stage exposes the internal service |
| TK-R02 | Existing low-level MCP writes do not acquire the new project lease | TaskService stays internal; Stage 3 blocker | all public writes share one project/session ownership policy before task tools ship |
| TK-R03 | OCCT or FreeCAD process crash still takes down the in-process control plane | accepted; Worker phase | isolated FreeCAD Worker with timeout/cancel/crash recovery |
| TK-R04 | R-A10 closeDocument side-effect-then-raise remains theoretically ambiguous | isolate candidate and treat close as cleanup; Worker phase | document lifecycle crosses a supervised worker boundary |
| TK-R05 | FCStd clone does not preserve pre-base undo/redo history or volatile labels | accepted/documented; CAD capability phase | durable semantic history/label regeneration contract is implemented |
| TK-R06 | R-B16 stable semantic tool error taxonomy is incomplete | zero retries; narrow needs_input policy | versioned tool codes and tested repair policy exist before automatic repair |
| TK-R07 | Windows advisory-lock and directory-fsync behavior is not executable on the current macOS host | implementation branch may exist but stays unclaimed | Windows CI/host gate proves contention and durability or platform is declared unsupported |
| TK-R08 | Network filesystems are outside the durability contract and cannot be identified portably from every Python host | require an explicitly trusted local root and reject observed uncertainty | explicit supported network-store transaction design and tests |
| TK-R09 | JSON checksum detects corruption but not same-user malicious tampering | accepted local trust boundary | authenticated multi-user storage/threat model requires it |
| TK-R10 | TaskRun and project revision stores are separate durability domains | commit journal + reconcile; Stage C closes ambiguity, not atomic co-transaction | later database/transaction design unifies them if product requirements demand |
| TK-R11 | Task ingress can cap an already-constructed ModelProgram but cannot prevent memory spent constructing hostile C1 containers | service remains internal | Stage 3 raw MCP ingress enforces byte/node/string limits before contract construction |
| TK-R12 | Preservation, assembly, feature inference, format-deep-validation, and visual checks are not supported | fail closed; later CAD capability stages | each check gains a typed observation, deterministic test, and approved allowlist entry |
| TK-R13 | Same-process trusted handlers can retain a Session reference | accepted internal boundary aligned with R-B19 | isolated Worker removes callable/session aliasing |
| TK-R14 | SessionSlot is injected and not server._session | intentional; Stage 3 | server composition adopts the slot without duplicating domain logic |
| TK-R15 | Only the existing four default registry operations are executable | intentional narrow slice | separately reviewed operation expansion with validation and eval |
| TK-R16 | No retry, repair/replan, cancellation, timeout interruption, Sampling, or BYOK | intentional | separately approved stages implement each bounded policy |
| TK-R17 | R-B20 is closed only for the initial deterministic check matrix | C5 evidence cannot verify; new verifier owns listed checks | wider criteria use trusted observations and pass adversarial review |
| TK-R18 | R-B21 caller-owned revision is superseded only inside TaskService | candidate coordinator owns internal revisions; C5 API stays compatible | all public task execution routes through coordinator in Stage 3 |
| TK-R19 | R-A11 lowercase origin redirects to canonical VibeCAD.git | do not mutate user remote | user separately approves remote normalization |
| TK-R20 | PR creation is not authorized | intentional | user separately authorizes publication |

## Recovery Snapshot

- Planning anchor is clean and pushed:
  bf8967077addfbd8002d6289b0eb925f3f55b638.
- Before approval, no production branch, runtime artifact, CAD process, install,
  stage, commit, push, or PR is authorized.
- After approval, create codex/task-kernel-phase2 from the exact anchor. If the
  anchor or working tree has changed unexpectedly, stop and revise this record.
- Every accepted pushed commit is a recovery point. Never reset, discard, amend,
  force-push, or rewrite user work.
- On a failed gate, leave evidence and allowlisted work available for diagnosis;
  do not hide it with destructive Git commands.
- Test runtime stores must be created under pytest temporary roots. A failed
  test may clean only paths it created and owns; it must never scan/delete user
  projects.
- Runtime recovery uses HEAD + immutable manifest + commit journal. It never
  guesses from TaskRun status alone and never rolls an advanced HEAD backward.

## Design Review Evidence

Three independent read-only reviews were completed at the clean anchor:

| Review | Main finding adopted |
|---|---|
| TaskRun/store/verifier | strict state invariants, CAS+fsync store, fail-closed trusted observations, and commit-receipt reconciliation are required |
| candidate/session | do not mutate committed Session and reload for rollback; use immutable base, isolated Session, HEAD linearization, receipt binding, and SessionSlot CAS |
| Stage slice | preserve single-semantic commits, keep server/C1-C5 unchanged, reuse the C5 adapter, and prove create/modify/verify/export plus hash-stable rollback on real FreeCAD |

No reviewer edited files, ran CAD, installed software, staged, committed, pushed,
or used the network.

## Authorization Record

- TK-A00 — 2026-07-16 user instruction “开始吧”: planning and preparation of
  TK-R1 only; implementation not yet authorized because the concrete revision
  did not yet exist.
- TK-A01 — pending.

- TK-A02 — 2026-07-17. The user supplied the exact words:
  “批准 TK-R1，按计划开始 Stage C。” This supersedes the TK-A01 placeholder
  and approves TK-R1 decisions TK-D01–TK-D24, commits TK1–TK9, the exact file
  allowlist, gates, budgets, circuit breakers, non-force branch pushes, and
  recovery discipline. It does not authorize a pull request, main push, remote
  rewrite, install/upgrade, public MCP exposure, model access, or any
  out-of-allowlist change. The same exact approval was repeated once; the
  repetition adds no authority and requires no second approval.

Required approval wording:

    批准 TK-R1，按计划开始 Stage C。

Approval authorizes only the branch, files, commits, gates, non-force pushes,
and recovery behavior in TK-R1. Any material change requires TK-R2 and another
explicit approval.

## Execution Start — TK-S001

### 1. Completed milestones

- TK-R1 was approved under TK-A02.
- The controller re-read the orchestrated-execution skill and its planning,
  delegation, gates, ledger, and Codex adapter references before production
  implementation.
- The repository and upstream anchor were both verified as
  bf8967077addfbd8002d6289b0eb925f3f55b638.
- The approved artifact hash before this execution-start append was
  668ac913af69fe996daceb73110dd6da4b4765c34ecd3453a65681d431d9516a.
- Branch codex/task-kernel-phase2 was created from the exact anchor. No
  production source, test, stage, commit, push, install, CAD process, or
  network action had occurred at this snapshot.

### 2. Next steps

1. Run the unchanged Phase 1 baseline gates.
   - If the baseline differs from the accepted 389 C1-C5 compatibility tests
     or 882 normal tests for an unexplained reason, stop TK1 as an unexpected
     gate red.
2. Execute packet TK1-P1: focused RED, minimum state-contract implementation,
   focused GREEN, cumulative/compatibility/import/static gates.
   - If RED is not the intended missing-module failure, stop without production
     implementation.
   - If an out-of-allowlist write appears, stop and preserve evidence.
3. Assign a distinct deep read-only review.
   - If P0/P1 is nonzero, do not stage; repair only within TK1 and the two-attempt
     budget.
   - If a P2 is not explicitly closed or dispositioned, do not stage.
4. Controller verifies evidence, appends the ledger, stages named files only,
   commits the prewritten TK1 message, pushes non-force, and verifies
   HEAD/upstream equality.

### 3. Approved decisions

- Active revision: TK-R1.
- Authorization: TK-A02, exact user wording
  “批准 TK-R1，按计划开始 Stage C。”
- Active decisions: TK-D01–TK-D24.
- Current packet authority: TK1 only; later packets remain ordered and approved
  but cannot start until TK1 is independently accepted, committed, and pushed.

### 4. Execution discipline

- Capability profile: native-plan / spawn-send-wait / repo-artifact /
  native-session-poll.
- Adapter: Codex; the repository artifact is authoritative and native plan is a
  projection.
- TK1 write scope: src/vibecad/workflow/state.py,
  tests/test_task_state.py, and this controller-owned artifact.
- Gates: genuine focused RED; focused GREEN; cumulative TK1; C1-C5
  compatibility; pure import; Ruff lint/format; diff/allowlist; independent
  review; staged gates; exact commit and immediate push.
- Circuit breakers: unexpected red, baseline mismatch, out-of-allowlist write,
  process ambiguity, third GREEN attempt, P0/P1, unresolved P2, or any need to
  alter the approved architecture/authority.
- Recovery checks: git status --short --branch; git rev-parse HEAD; compare
  upstream when configured; inspect named-file diffs; verify this artifact and
  resume only from its next observable branch.

## Task Packet TK1-P1

### 1. Authorization

TK-R1 and decisions TK-D01–TK-D05, TK-D20, and TK-D24 are approved by TK-A02.
This packet inherits all higher-priority system, developer, and user
instructions, any applicable directory-scoped AGENTS.md/CLAUDE.md, the approved
file allowlist, and the current host permission model and sandbox. The Skill,
approved artifact, and this packet cannot grant or expand permissions, elevate
authority, or bypass that model or sandbox. Do not request the same approval
again.

### 2. Workspace anchor

- Repository: /Users/wangtao/Documents/DevProject/vibecad.
- Branch: codex/task-kernel-phase2.
- Commit anchor: bf8967077addfbd8002d6289b0eb925f3f55b638.
- No repository-scoped AGENTS.md or CLAUDE.md was observed.
- Implementer may modify only:
  src/vibecad/workflow/state.py and tests/test_task_state.py.
- The controller alone updates
  docs/orchestrated/vibecad-task-kernel-phase2.md.
- Every other path, including existing C1-C5 source/tests, server, Session,
  tools, dependencies, runtime, manifest, and Git metadata mutation, is
  prohibited. The current host permission model and sandbox remain binding.

### 3. Context

Create the durable, provider-neutral TaskRun state contract below the model
layer. It must persist only strict schema-v1 data and the submitted
ModelProgram, never ValidatedProgram, a handler, filesystem path, Session,
FreeCAD object, MCP object, or model-provider value. It must enforce canonical
task/project/revision/artifact identifiers, cross-field invariants, bounded
history, terminal-state protection, and deterministic next actions. Phase 2
service policy will later activate external_plan only, but the contract may
represent all three accepted reasoning-owner enum values.

Known failure modes are forged/malformed mappings, duplicate or inconsistent
sequence data, program/task/base mismatch, candidate states without a candidate
revision, success without a passing verification and committed revision,
failure without a structured error, candidate-bearing failure that bypasses
rolling_back, mutable caller containers, unbounded histories, and accidental
imports or side effects.

### 4. Steps and gates

1. Inspect the existing C1 contracts and errors read-only; do not change them.
2. Add the minimum focused tests first and run:

       PYTHONPATH=src .venv/bin/pytest -q tests/test_task_state.py

   The only acceptable genuine RED is collection failure because
   vibecad.workflow.state does not exist. Preserve the exact command, exit
   status, and missing-module signature. Any syntax/setup/import-path or other
   failure stops the packet.
3. Implement only src/vibecad/workflow/state.py with the TK1 public types and
   pure transition/append/round-trip functions named in TK-R1. Use immutable
   dataclasses/enums, strict mappings, stable non-reflective errors, defensive
   copies, and hard budgets. Do not add timestamps or persistence I/O.
4. Run the focused GREEN command above; then run:

       PYTHONPATH=src .venv/bin/pytest -q \
         tests/test_task_state.py \
         tests/test_workflow_contracts.py \
         tests/test_execution_registry.py \
         tests/test_model_program.py \
         tests/test_tool_result_normalizer.py \
         tests/test_execution_adapter.py

5. Prove pure import in a fresh interpreter without FreeCAD, Part, mcp, model
   SDK, filesystem write, or network initialization; run Ruff check and format
   check on the two packet files; run git diff --check and inspect the exact
   allowlist.
6. Return the dirty implementation for controller verification. Do not edit
   this artifact, stage, commit, push, install, run CAD, access the network,
   rewrite history, or start TK2.

Expected impact is one new pure module and one new focused test file. The
post-commit revert command is git revert <TK1-commit>; only the controller may
use it, and no revert is authorized unless later evidence requires it.

### 5. Execution discipline

- Delegation: spawn-send-wait.
- Model tier: standard; selected adapter mapping gpt-5.6-terra/high.
- Process: native-session-poll when a live command session is returned;
  otherwise one bounded blocking command. Never relaunch a live process.
- Implementation budget: two bounded GREEN attempts.
- Stop on the first unexpected gate red, baseline mismatch, out-of-allowlist
  write, native exception leakage, import side effect, process ambiguity, or
  need to change an approved contract/allowlist.
- Preserve RED and any failed attempt evidence. Do not opportunistically fix an
  out-of-scope issue.

### 6. Delivery boundary

Complete the test-first implementation and declared implementer gates only.
The controller reserves artifact/ledger edits, independent reviewer assignment,
finding disposition, acceptance, exact staging, commit, push, recovery
snapshot, and the decision to begin TK2.

### 7. Final report

Return:

- exact files changed and their SHA-256 hashes;
- genuine RED command, exit status, and observed missing-module signature;
- implemented API/enums/error codes/budgets and transition matrix summary;
- focused and cumulative commands with exit status and numeric counts;
- pure-import, forbidden-module, Ruff, format, diff, and allowlist evidence;
- deviations, residuals, and any failed attempts;
- confirmation of no artifact edit, stage, commit, push, install, CAD, network,
  or out-of-allowlist action;
- final branch, HEAD, and git status.

## TK1-P1 Blocked Evidence — TK1-E001

| Entry ID | Decision / approval | Commit / push | Gate evidence | Residual | Snapshot | State |
|---|---|---|---|---|---|---|
| TK1-E001 / 2026-07-17 | TK-D01–TK-D05, TK-D20, TK-D24 at TK-R1; TK-A02 | not-created / not-pushed | baseline: 389 passed, 1 deselected and 882 passed, 81 deselected; genuine RED exit 2 missing vibecad.workflow.state; GREEN attempt 1 exit 1, 26 passed/5 failed; GREEN attempt 2 exit 1, 30 passed/1 failed | TK1-B01–TK1-B06 | TK-S002 | blocked |

The genuine RED was:

    PYTHONPATH=src .venv/bin/pytest -q tests/test_task_state.py

It exited 2 during collection solely with:

    ModuleNotFoundError: No module named 'vibecad.workflow.state'

GREEN attempt 1 used the same command and exited 1 with 26 passed and 5
failed. The five failures were bounded test/state-flow mismatches. The worker
corrected only those focused fixtures within TK1.

GREEN attempt 2 used the same command and exited 1 with 30 passed and 1 failed.
The remaining failure proves that VALIDATE_PROGRAM without candidate_revision
returns INVALID_TYPE while the contract test requires INVALID_IDENTIFIER.
The implementation's later explicit missing-identifier branch is unreachable.
The same root exists for COMMIT without committed_revision.

The two-attempt packet budget was exhausted. No third test run, cumulative
gate, pure-import gate, Ruff/format gate, review repair, stage, commit, or push
was attempted.

Dirty implementation hashes at the breaker:

- src/vibecad/workflow/state.py:
  bf853d17efaff84f3a52334d61b6934c8e8231ca1501722accbd08920aec6e30
- tests/test_task_state.py:
  6fc703c7ffda41ef4f85be45acbf5ffbda1b32189c915d3e9e72c59d0e5df61c
- controller artifact after TK1-P1 issuance and before this blocked append:
  c61dabba6263946164031564fec856b31356cced89cf82f3af96b71ecd2b25ab

The dirty scope is exactly those two untracked packet files plus this untracked
controller artifact. Branch and HEAD remain
codex/task-kernel-phase2@bf8967077addfbd8002d6289b0eb925f3f55b638.

### Independent blocked review

The distinct deep reviewer returned REJECT with P0=0, P1=4, and P2=2:

- TK1-B01 / P1 — Required candidate_revision and committed_revision use the
  wrong missing-value precedence; the tests are correct and the implementation
  contains unreachable intended guards.
- TK1-B02 / P1 — TaskRun cross-field invariants do not reject contradictory
  durable candidate/evidence/error combinations.
- TK1-B03 / P1 — Nested histories are fully parsed before their hard budget is
  checked; VerificationReport verdicts have no hard count budget.
- TK1-B04 / P1 — The test matrix is not yet exhaustive across status/event,
  malformed nested mappings, every history budget, duplicate IDs, sequences,
  and cross-field invariants.
- TK1-B05 / P2 — Nested contract errors are not consistently translated with
  their full parent JSON Pointer, some hostile values can leak native
  exceptions, and unknown-field path rendering is not safely escaped/quoted.
- TK1-B06 / P2 — CriterionVerdict reports an invalid required field at /passed,
  and the two files have not passed the required Ruff/format gate.

The reviewer made no edit, test/CAD run, Git write, install, network action, or
scope expansion.

## Recovery Snapshot — TK-S002

### 1. Completed milestones

- TK-R1 remains approved under TK-A02.
- codex/task-kernel-phase2 remains at the clean committed anchor
  bf8967077addfbd8002d6289b0eb925f3f55b638.
- The unchanged baseline passed 389 C1-C5 compatibility tests and 882 normal
  tests.
- TK1 genuine RED was proven and preserved.
- Two bounded GREEN attempts were executed and preserved; the second ended at
  30 passed/1 failed.
- Independent review is REJECT, P0=0/P1=4/P2=2.
- No Stage C commit or push exists.

### 2. Next steps

1. Complete bounded read-only contract-completeness review for dependencies
   already approved in TK4/TK8.
2. Append proposed TK-R2 with the same architecture, commit count, and
   allowlist but an explicit repair scope and renewed attempt budget.
3. Wait for explicit TK-R2 approval.
   - If approved exactly, issue TK1-RP1 and resume from the preserved dirty
     files without repeating the original missing-module RED.
   - If not approved, leave all three untracked files intact and Stage C
     blocked.
4. After an approved repair reaches GREEN, run all deferred TK1 gates and a
   new distinct read-only acceptance review before any staging.

### 3. Approved decisions

- TK-R1 decisions TK-D01–TK-D24 and TK-A02 remain active.
- TK-A02 does not authorize a third GREEN attempt after the TK1-P1 circuit
  breaker or an expanded repair packet.
- No architecture, file allowlist, commit message/count, external authority, or
  later packet has changed.

### 4. Execution discipline

- Capability profile remains native-plan / spawn-send-wait / repo-artifact /
  native-session-poll on the Codex adapter.
- Only controller artifact writes and read-only inspection are permitted while
  blocked.
- Production/test repair, any further test command, review repair, stage,
  commit, push, install, CAD, and network actions are frozen pending a new
  approved revision.
- Recovery sequence: verify branch and HEAD; verify the three dirty paths and
  hashes; read TK1-E001/TK-S002; verify the next authorization; never discard or
  reset the preserved attempt.

## Proposed Revision TK-R2 — Not Authorized

- Proposal state: awaiting explicit user approval.
- Proposed active revision after approval: TK-R2.
- Current execution state: TK1 blocked; all production/test work and tests are
  frozen.
- Proposal base artifact SHA-256:
  940f9ba1bbed6723bf896042a4f935991c5926769a901a5a6dbde4d40d5dd569.
- Preserved source SHA-256:
  bf853d17efaff84f3a52334d61b6934c8e8231ca1501722accbd08920aec6e30.
- Preserved test SHA-256:
  6fc703c7ffda41ef4f85be45acbf5ffbda1b32189c915d3e9e72c59d0e5df61c.

TK-R2 does not change the Stage C architecture, nine-commit budget, commit
sequence or messages, target branch, total file allowlist, external authority,
public API boundary, model policy, installed FreeCAD policy, push policy, or
pull-request policy. It closes contract defects and approved downstream
dependency gaps inside the already planned TK1 semantic commit, and grants a
new bounded repair budget only after approval.

No statement in this proposal authorizes code/test repair, another test run,
staging, commit, push, install, CAD execution, network access, or TK2. Until the
approval record below is completed, TK-R1/TK-A02 and the TK-S002 freeze remain
the only authority.

### Revision drivers

| ID | Severity | Blocking evidence | Required closure in TK1 |
|---|---|---|---|
| TK1-B01 | P1 | Missing required candidate_revision and committed_revision return INVALID_TYPE before the intended required-identifier guard | establish required identifier precedence and cover both fields |
| TK1-B02 | P1 | A forged durable TaskRun can contradict its transition provenance, candidate data, verification, artifacts, and error state | enforce complete cross-field and history-derived invariants |
| TK1-B03 | P1 | Nested histories are parsed before their hard limits and verdicts/evidence have no complete bound | fail before nested parsing and cover every declared sequence budget |
| TK1-B04 | P1 | Tests do not exhaust the status/event matrix, malformed nested records, budgets, duplicates, sequence rules, and invariants | add a table-driven exhaustive/adversarial state-contract suite |
| TK1-B05 | P2 | Nested contract errors lose parent paths; hostile mappings can leak native exceptions; error rendering can admit unsafe paths/log text | use canonical pointer composition, safe rendering, and stable translated errors |
| TK1-B06 | P2 | The required-field pointer is wrong and Ruff/format gates have not run | correct field-local paths and pass all static gates |
| TK1-B07 | P1 | TK7/TK8 need to record controlled artifacts, but their named-file scopes cannot later add the missing TaskRun operation | add revision-bound append_artifact now |
| TK1-B08 | P1 | The current boolean/message verdict cannot durably carry TK4 expected, observed, delta, tolerance, evidence, unsupported semantics, or AcceptanceSpec identity | complete the provider-neutral verifier result contract now |
| TK1-B09 | P1 | RECOVERY_REQUIRED and CLEANUP_REQUIRED have no legal exit although TK8 must reconcile both committed and uncommitted outcomes | add explicit, evidence-gated reconciliation transitions |
| TK1-B10 | P1 | CREATED derives SUBMIT_PROGRAM even though its only legal transition is REQUEST_PLAN | add REQUEST_PLAN as the deterministic next action |

### Clarifying decisions

- TK-D25 — Required identifiers have stable precedence. For a required
  identifier, omission or null returns INVALID_IDENTIFIER at the exact field;
  a non-string non-null value returns INVALID_TYPE; and a malformed string
  returns INVALID_IDENTIFIER. Optional identifiers alone accept null. This
  applies to candidate_revision, committed_revision, and equivalent helpers.
- TK-D26 — A TaskRun must be reconstructible from its contiguous legal
  transition history, and its fields must agree with that provenance. Data that
  originates from a candidate cannot exist before candidate creation. A
  candidate revision is immutable once created; steps, reports, and artifacts
  must bind to it. COMMITTING and SUCCEEDED require a passing report for the
  current candidate; only SUCCEEDED may hold a committed revision. Candidate
  failure must pass through ROLLING_BACK. Candidate-origin NEEDS_INPUT,
  RECOVERY_REQUIRED, CLEANUP_REQUIRED, and FAILED retain their candidate
  identity/evidence and a structured error. NEEDS_INPUT requires
  last_error.needs_input=true. Resubmission clears the prior error. Forged
  status/field/history combinations fail closed.
- TK-D27 — Parsing checks outer sequence type and size before constructing any
  nested item. Existing limits remain 64 step records, 128 transition records,
  16 verification reports, and 128 task artifacts. TK-R2 adds at most 128
  CriterionVerdict values per VerificationReport and at most 32 evidence
  references per CriterionVerdict. Exact list/tuple inputs are defensively
  copied; oversize input must fail without touching a nested sentinel.
- TK-D28 — State mapping parsers accept strict JSON-object dictionaries and
  never reflect hostile field content in messages. All paths use
  join_json_pointer-compatible RFC 6901 escaping. TaskStateError renders path
  and message with JSON quoting and remains bounded and single-line.
  ContractValidationError from ModelProgram, StepResult, or StepError parsing is
  translated to the corresponding stable TaskStateError with the full parent
  pointer. Malformed admissible input must not leak KeyError, TypeError,
  AttributeError, Unicode/rendering errors, or iterator exceptions.
- TK-D29 — Add CriterionOutcome with pass, fail, and unsupported values.
  CriterionVerdict durably records criterion_id, required, outcome, expected,
  observed, delta, tolerance, bounded canonical evidence pointers into the
  trusted ObservationSnapshot, and a bounded message. VerificationReport adds
  acceptance_id and remains bound to candidate_revision, manifest_sha256, and
  observation_digest. Its passed flag equals “every required verdict has pass
  outcome”; a required unsupported verdict fails, while optional unsupported
  does not alone fail the report. These mapping contracts are immutable,
  strict, JSON-compatible, finite, bounded, and provider-neutral. Receipt
  authenticity remains TK4 responsibility; TK1 imports no validation module.
- TK-D30 — TaskArtifactRef adds the candidate revision it describes and never
  contains a filesystem path. Add append_artifact(task, artifact), legal only
  while the current candidate is EXECUTING or VERIFYING. It requires exact
  candidate binding, a unique artifact ID, and the existing 128-item budget;
  the function returns a new TaskRun and cannot mutate prior state.
- TK-D31 — Add CONFIRM_COMMITTED and CONFIRM_UNCOMMITTED reconciliation events
  from both RECOVERY_REQUIRED and CLEANUP_REQUIRED. CONFIRM_COMMITTED reaches
  SUCCEEDED only with an explicit committed revision and an already durable
  passing report bound to the current candidate. CONFIRM_UNCOMMITTED reaches
  ROLLING_BACK, after which existing COMPLETE_ROLLBACK or REQUEST_INPUT policy
  applies; it never guesses or repeats CAD side effects. Add
  NextAction.REQUEST_PLAN for CREATED; NEEDS_PLAN continues to derive
  SUBMIT_PROGRAM.
- TK-D32 — Persisting the user Intent in TaskRun is explicitly deferred and is
  not a TK1 blocker. ModelProgram already binds task ID, base revision, and
  AcceptanceSpec for the approved external_plan Stage C path. Intent remains a
  Stage 3 ingress/audit design choice and is not added by TK-R2.

### Authority and budget delta

- If approved, TK-R2 supersedes TK-R1 only where TK-D25–TK-D32 and TK1-RP1 are
  more specific. TK-D01–TK-D24 otherwise remain active.
- The preserved missing-module RED remains valid evidence and is not repeated.
- TK1-RP1 first expands tests and runs one repair RED. The repair RED is a gate,
  not a GREEN attempt. It must expose only the declared TK1-B01–TK1-B10
  signatures; setup, collection, import-path, or unrelated failures stop work.
- TK-R2 grants exactly two new bounded GREEN attempts for the TK1 repair. If the
  second does not pass, work stops for TK-R3. It does not borrow budget from a
  later commit.
- After focused GREEN, every deferred TK1 cumulative, compatibility,
  pure-import, forbidden-import, Ruff check, Ruff format, diff, allowlist, and
  staged-content gate is mandatory.
- A new distinct deep read-only reviewer must return ACCEPT with P0=0/P1=0.
  Every P2 must be closed or explicitly dispositioned before staging.
- The controller may stage only the three original TK1 named files, create the
  original commit `feat(workflow): define durable task-run state contracts`,
  push it non-force, and start TK2 only after all gates pass.

## Task Packet TK1-RP1 — Proposed, Inactive Until TK-R2 Approval

### 1. Authorization

This repair packet becomes active only if the user explicitly approves TK-R2
under TK-A03. It then inherits TK-R1, TK-D01–TK-D32, all higher-priority
system/developer/user instructions, the approved file allowlist, and the host
permission model. It grants no pull request, main push, remote rewrite,
installation, model/network access, public MCP exposure, arbitrary code, or
out-of-allowlist action.

### 2. Workspace anchor

- Repository: /Users/wangtao/Documents/DevProject/vibecad.
- Branch: codex/task-kernel-phase2.
- Committed anchor: bf8967077addfbd8002d6289b0eb925f3f55b638.
- Resume the preserved untracked files at the exact hashes recorded above; do
  not recreate, reset, discard, or replace their history.
- Implementer write scope:
  src/vibecad/workflow/state.py and tests/test_task_state.py only.
- Controller-only path:
  docs/orchestrated/vibecad-task-kernel-phase2.md.
- Every other source, test, dependency, runtime file, Git metadata mutation,
  and environment change is prohibited for the implementer.

### 3. Context

Close TK1-B01–TK1-B10 without changing Stage C architecture or starting TK2.
The durable state contract is an untrusted-data boundary for later TaskRun
storage and the sole type dependency available to TK4, TK7, and TK8 under their
approved named-file scopes. It must therefore reject forged persisted records,
bound nested work before parsing it, preserve full diagnostic paths without log
forging, carry independent verifier evidence, record controlled artifacts, and
express deterministic recovery exits now.

### 4. Steps and gates

1. Extend tests first. Import the state module as a module and perform lookup of
   newly proposed symbols inside test bodies so the repair RED collects the
   suite instead of stopping at a new-symbol import error.
2. Add exhaustive/table-driven coverage for every status/event pair; required
   identifier precedence; transition/step/report/artifact sequences and
   duplicates; every hard budget with fail-before-nested-parse sentinels;
   malformed/hostile mappings and fully prefixed escaped pointers; native-error
   containment; immutable round trips; cross-field/history forgeries; rich
   verdict/report semantics; append_artifact; both recovery outcomes; and every
   next action.
3. Run exactly one repair RED:

       PYTHONPATH=src .venv/bin/pytest -q tests/test_task_state.py

   Expected signatures are the preserved candidate/commit identifier mismatch,
   accepted contradictory records, missing fail-fast verdict/evidence limits,
   unsafe or unprefixed nested errors, absent append_artifact/rich verifier
   schema/reconciliation events, and CREATED next-action mismatch. B04 closes
   through the expanded test matrix and B06 through later static gates. Any
   setup, collection, import-path, or unrelated regression stops the packet.
4. Implement the minimum pure state-contract repair for TK-D25–TK-D31. Do not
   add Intent, persistence, lease, filesystem, CAD, MCP, model, receipt,
   verifier-engine, clock, random-ID generation, or network behavior.
5. Run the focused command. At most two GREEN attempts are available. On GREEN,
   run the cumulative TK1/C1–C5 command:

       PYTHONPATH=src .venv/bin/pytest -q \
         tests/test_task_state.py \
         tests/test_workflow_contracts.py \
         tests/test_execution_registry.py \
         tests/test_model_program.py \
         tests/test_tool_result_normalizer.py \
         tests/test_execution_adapter.py

6. Prove pure import in a fresh interpreter without FreeCAD, Part, mcp, a model
   SDK, filesystem write, CAD initialization, or network initialization. Run
   Ruff check and format check on the two packet files, git diff --check, exact
   dirty-path/allowlist inspection, and mutation/round-trip readback.
7. Return the dirty work to the controller. The controller assigns a new
   distinct deep read-only review. Only after ACCEPT with P0=0/P1=0 and all P2
   closed/dispositioned may the controller append acceptance evidence, stage
   the exact three TK1 files, rerun staged gates, commit the original TK1
   message, push non-force, and verify HEAD equals upstream.

### 5. Execution discipline

- Delegation: spawn-send-wait; standard implementer mapping
  gpt-5.6-terra/high and distinct deep reviewer mapping gpt-5.6-sol/high.
- Process: native-session-poll for any returned live session; never relaunch or
  duplicate a live command.
- Test-first repair RED plus exactly two bounded GREEN attempts.
- Stop on unexpected RED, a second failed GREEN, new P0/P1, unresolved P2,
  native exception leakage, import side effect, out-of-allowlist write, process
  ambiguity, architecture change, or need for a tenth commit.
- Preserve all evidence. Never reset, discard, amend, force-push, hide a failed
  attempt, install/upgrade software, execute CAD, or opportunistically change
  an existing C1–C5 module.

### 6. Delivery boundary

Deliver only the complete TK1 repair and declared gates. The controller owns
artifact/ledger changes, finding disposition, acceptance, exact staging,
commit, push, recovery snapshot, and authorization to start TK2. The original
TK1 semantic commit and nine-commit campaign budget remain unchanged.

### 7. Final report

Return exact changed-file hashes; test additions; repair RED command/status and
signature mapping to blockers; both GREEN-attempt records; focused and
cumulative numeric results; pure/forbidden import evidence; Ruff/format/diff
and allowlist evidence; transition/invariant/budget summary; deviations and
residuals; confirmation of no artifact edit/stage/commit/push/install/CAD/
network/out-of-scope action; and final branch, HEAD, and git status.

## TK-R2 Authorization Record

- TK-A03 — pending. No TK-R2 implementation authority exists yet.

Required approval wording:

    批准 TK-R2，关闭 TK1-B01 至 TK1-B10 后继续 Stage C。

That wording approves only TK-D25–TK-D32, TK1-RP1, its renewed two-attempt
budget, the original TK1 commit/push after acceptance, and continuation of the
already approved TK2–TK9 sequence. Any different architecture, file, commit,
model, public API, install, remote, or publication action still requires a new
revision and approval.

## Recovery Snapshot — TK-S003

### 1. Completed milestones

- TK-R1/TK-A02 started Stage C at the exact approved anchor.
- Baseline, genuine TK1 RED, two failed GREEN attempts, the circuit breaker,
  and the independent REJECT review are preserved in TK1-E001/TK-S002.
- Read-only dependency review identified TK1-B07–TK1-B10 before TK4/TK7/TK8.
- TK-R2 and TK1-RP1 are now fully specified but remain unauthorized.
- No Stage C commit or push exists.

### 2. Next steps

1. Wait for the exact TK-A03 approval wording.
2. If approved, verify branch, HEAD, dirty paths, and all four pre-repair hashes;
   append TK-A03 and activate TK1-RP1.
3. Extend tests, run the repair RED, then use no more than two GREEN attempts.
4. Run all deferred gates and a distinct independent review before any staging.
5. If approval is absent or differs materially, preserve the three untracked
   files and remain blocked.

### 3. Approved decisions

- TK-D01–TK-D24 remain approved by TK-A02.
- TK-D25–TK-D32 and TK1-RP1 are proposed only; TK-A03 is pending.
- The architecture, target branch, exact allowlist, nine commits, original TK1
  message, immediate non-force push policy, and no-PR boundary are unchanged.

### 4. Execution discipline

- Capability profile: native-plan / spawn-send-wait / repo-artifact /
  native-session-poll; repository artifact remains authoritative.
- While awaiting approval, only artifact readback and non-mutating recovery
  inspection are allowed.
- Do not edit production/tests, run tests, stage, commit, push, install, execute
  CAD, access the network, or start TK2.
- Resume only from observable branch/HEAD/hash evidence and the explicit TK-A03
  authorization; never infer approval from TK-R1 or from silence.

## TK-R2 Activation — TK-A03

- TK-A03 — 2026-07-17. The user supplied the exact words:
  “批准 TK-R2，关闭 TK1-B01 至 TK1-B10 后继续 Stage C。”
- The approved TK-R2 proposal artifact SHA-256 was
  571a39690b2c5b576e3f2b9ceeea8c994ae28be63cd67967af4e51e55ddfae07.
- Branch, committed anchor, dirty paths, source hash, and test hash were
  reverified and exactly matched TK-S003 before activation.
- TK-D25–TK-D32, TK1-RP1, and exactly two renewed GREEN attempts are now
  authorized. TK-R1 decisions remain active where TK-R2 is not more specific.
- No implementation/test action occurred between the proposal and approval.

The user also observed that this interruption presented no meaningful product
choice and did not explain its user-level effect. That observation is adopted
as execution-process guidance, not as an expansion of technical authority:

- Internal repair work that remains within an already approved product,
  security, data, dependency, cost, branch, and file boundary should be given a
  sufficiently broad bounded repair envelope in the original revision.
- Future approval requests must state the concrete user-facing choice or risk.
  If no such choice exists but an explicit gate is mandatory, the request must
  say that it is a process authorization and explain why it cannot be handled
  internally.
- User interruption remains reserved for material product behavior,
  architecture boundary, security/data policy, external cost/action,
  publication, or a mandatory circuit breaker that cannot safely be delegated.

The TK-R2 user-level effect is to prevent false task success, permanently stuck
recovery/cleanup states, and STEP/FCStd evidence that cannot be tied to the
candidate revision and AcceptanceSpec. It adds no feature surface, install,
provider, token use, external charge, or public API.

## Recovery Snapshot — TK-S004

### 1. Completed milestones

- TK-R2 is approved under TK-A03 at the exact preserved branch and hashes.
- TK1-B01–TK1-B10 and TK-D25–TK-D32 are authoritative repair scope.
- TK1-RP1 is active with its test-first repair RED and two GREEN attempts.
- No Stage C commit/push, install, CAD execution, or out-of-scope write exists.

### 2. Next steps

1. Delegate TK1-RP1 to a bounded standard implementation worker.
2. Extend tests first and preserve the expected repair RED signatures.
3. Implement within the two-file worker scope and use no more than two GREEN
   attempts.
4. On GREEN, run every cumulative/static/import/diff gate and obtain a new
   distinct deep ACCEPT review before staging.

### 3. Approved decisions

- TK-R1/TK-A02 and TK-R2/TK-A03 are active; TK-R2 is more specific for TK1.
- Active packet: TK1-RP1 only.
- Approved commit remains
  `feat(workflow): define durable task-run state contracts` with the original
  three named files and immediate non-force push after acceptance.
- The user's process feedback changes future approval communication and budget
  design; it does not waive mandatory safety or scope circuit breakers.

### 4. Execution discipline

- Capability profile: native-plan / spawn-send-wait / repo-artifact /
  native-session-poll; repository artifact remains authoritative.
- Worker may edit only state.py and test_task_state.py. Controller alone edits
  this artifact and owns review, stage, commit, push, and TK2 activation.
- Stop on unexpected repair RED, second failed GREEN, P0/P1, unresolved P2,
  native exception leakage, out-of-allowlist change, process ambiguity, or
  architectural expansion.
- Preserve evidence; never reset/discard/amend/force-push, install, run CAD,
  access the network, or infer permission beyond TK-A03.

## TK1-RP1 Review Evidence — TK1-E002

| Entry ID | Decision / approval | Commit / push | Gate evidence | Residual | Snapshot | State |
|---|---|---|---|---|---|---|
| TK1-E002 / 2026-07-17 | TK-D25–TK-D32 at TK-R2; TK-A03 | not-created / not-pushed | repair RED: 30 passed/8 failed; GREEN 1: 34 passed/4 failed; GREEN 2: 38 passed; cumulative: 427 passed/1 deselected; pure import, forbidden import, Ruff check/format, and diff gates passed; independent review REJECT P0=0/P1=3/P2=1 | TK1-B02, TK1-B04, TK1-B05, and TK1-B08 remain open | TK-S005 | review-repair |

The independent reviewer confirmed that TK1-B01, TK1-B07, TK1-B09, and
TK1-B10 reached their focused behavior, but acceptance is blocked by four
findings already inside the explicitly approved TK1-B01–TK1-B10 outcome:

- TK1-B08 / P1 — CriterionVerdict expected/observed retain arbitrary mutable
  non-JSON aliases; numeric validation can leak OverflowError; contradictory
  outcome/passed inputs are silently normalized.
- TK1-B02 / P1 — candidate identity can change on a candidate-origin
  continuation; COMMITTING can be forged without a durable passing report;
  ROLLING_BACK/error and report/status provenance remain incomplete.
- TK1-B04 / P1 — the 38-test suite is not the required full status/event,
  budget/sentinel, duplicate, hostile mapping, invariant, verifier, artifact,
  and reconciliation matrix.
- TK1-B05 / P2 — TaskStateError rendering is not JSON-quoted/bounded and nested
  verdict failures lose their `/verdicts/<index>` prefix.

The review was read-only. The reviewer made no edit, test/CAD run, install,
network action, stage, commit, push, or Git metadata change.

This is not a new product, architecture, security/data, dependency, cost,
external-action, file, commit, or publication decision. TK-A03 explicitly
authorized the outcome “关闭 TK1-B01 至 TK1-B10”; TK1-E002 proves that outcome is
not yet complete. In accordance with the user's recorded process direction in
TK-A03, review closure continues internally rather than requesting a duplicate
approval for the same outcome. Any repair that would cross one of those
material boundaries still stops for user direction.

## Task Packet TK1-RP2 — Active Review Closure Under TK-A03

### 1. Authorization

TK-A03 remains the controlling outcome authorization. This packet closes only
the still-open TK1-B02, TK1-B04, TK1-B05, and TK1-B08 findings above, under
TK-D25–TK-D32 and the original TK1 named files. It does not alter Stage C
architecture, the nine-commit sequence, public API, external authority, exact
allowlist, installed environment, model policy, or push/publication policy.

The user's feedback explicitly rejects repeated participation in internal
correctness checkpoints with no user-level choice. This packet therefore uses
a distinct bounded review-closure budget inside the already approved outcome;
it does not infer authorization for any new outcome or external action.

### 2. Workspace anchor

- Repository: /Users/wangtao/Documents/DevProject/vibecad.
- Branch: codex/task-kernel-phase2.
- Committed anchor: bf8967077addfbd8002d6289b0eb925f3f55b638.
- Controller artifact before this append:
  d3f46255e61c8ec5988a498ec89256ec740655d6a069adcff077a3208895f466.
- Reviewed source:
  8a3814a006314fe41aaa9bff959117e6ec750c7a3bb1009f42ddfaa5e215725a.
- Reviewed tests:
  62e35bf279236b57b930c9e3c7df1a977b54ee8cc8e50a4f1c5e098d9e2070c2.
- Implementer may modify only src/vibecad/workflow/state.py and
  tests/test_task_state.py. The controller alone modifies this artifact.

### 3. Context

Focused GREEN is necessary but insufficient for an untrusted durable contract.
Close the independent findings without changing the public intent of TK1:

- freeze and thaw expected/observed with strict finite JSON semantics, safe
  integer/depth/node bounds, cycle rejection, and no caller aliases;
- make delta/tolerance null, a finite JSON-safe number, or a bounded finite
  numeric vector; require non-negative tolerance values;
- make outcome authoritative and remove the compatibility passed constructor
  input; make acceptance_id explicit and required;
- bind every StepResult.revision, VerificationReport, and TaskArtifactRef to the
  current candidate and the report to the submitted AcceptanceSpec;
- preserve candidate identity across candidate-origin continuation, require a
  passing report in COMMITTING/SUCCEEDED, and enforce phase/error provenance;
- JSON-quote and bound TaskStateError rendering, safely handle hostile unknown
  keys, and prefix nested verdict failures by exact index;
- prove all of this with the complete adversarial matrix before staging.

### 4. Steps and gates

1. Expand tests first. The test-only change must add the complete 13-status ×
   16-event expected matrix with legal payload guards and terminal precedence;
   exact-max/max+1 tests for all six declared sequence budgets; fail-before-
   nested-sentinel proof; real duplicate and sequence rejection; rich-value
   mutation/cycle/non-JSON/non-finite/unsafe-integer/depth/node cases;
   required/optional fail and unsupported semantics; StepResult/report/artifact
   candidate binding; AcceptanceSpec binding; candidate continuation identity;
   COMMITTING/error/report provenance forgeries; negative artifact and
   reconciliation paths; strict dict/list subclass rejection; hostile unknown
   keys and fully indexed nested error paths.
2. Run one review-repair RED:

       PYTHONPATH=src .venv/bin/pytest -q tests/test_task_state.py

   It must collect normally and fail only on the four independently documented
   findings. An environment/setup/import/unrelated failure stops the packet.
3. Implement the smallest pure state-contract closure. Reuse only pure
   standard-library behavior and existing workflow contract/error types. Do
   not change C1–C5 files or add persistence, validation-engine, CAD, MCP,
   model, filesystem, clock, ID generation, dependency, or network behavior.
4. The review-closure budget is at most three GREEN cycles after the declared
   RED. A failed third cycle freezes TK1. Each cycle uses the focused command;
   do not hide or overwrite prior evidence.
5. On focused GREEN, run the cumulative TK1/C1–C5 command from TK1-RP1, then
   fresh pure-import and forbidden-import checks, Ruff check, Ruff format,
   git diff --check, exact dirty-path/allowlist inspection, and mutation/
   round-trip readback. Remove any file-wide lint suppression used to conceal
   local formatting debt.
6. Assign a fresh distinct deep read-only acceptance reviewer. ACCEPT requires
   P0=0/P1=0 and every P2 closed or explicitly dispositioned. The reviewer must
   inspect both implementation and test completeness; passing tests alone are
   not acceptance.
7. Only after ACCEPT may the controller append final TK1 evidence, stage the
   exact three named files, rerun staged gates, create the original TK1 commit,
   push non-force, and verify clean HEAD/upstream equality before TK2.

### 5. Execution discipline

- Delegation: spawn-send-wait; standard implementation and deep review tiers.
- Process: native-session-poll for live sessions; never launch a duplicate.
- Budget: one test-first review RED, at most three review-closure GREEN cycles,
  then one fresh independent acceptance review.
- Stop on unexpected RED, failed third GREEN, P0/P1 after the fresh review,
  unresolved P2, native exception leakage, scope/architecture expansion,
  out-of-allowlist write, process ambiguity, or need for an extra semantic
  commit.
- Preserve evidence and user work. Never reset/discard/amend/force-push,
  install/upgrade, execute CAD, access network/model, or start TK2.

### 6. Delivery boundary

Deliver only the two-file TK1 review closure and its declared gates. Controller
ownership of artifact, acceptance, staging, commit, push, and TK2 remains
unchanged. No user-facing feature or public MCP tool is introduced.

### 7. Final report

Return changed hashes; exact new test categories; review-repair RED and every
GREEN attempt with counts/status; cumulative and static/import gates;
immutable JSON and provenance design summary; independent finding mapping;
deviations/residuals; no-prohibited-action confirmation; and final
branch/HEAD/status.

## Recovery Snapshot — TK-S005

### 1. Completed milestones

- TK1-RP1 reached focused/cumulative/static GREEN but independent review
  rejected it with P0=0/P1=3/P2=1.
- Every finding maps to already approved TK1-B02/B04/B05/B08; no new product or
  external decision exists.
- No Stage C commit/push or prohibited action exists.
- TK1-RP2 is active under TK-A03 outcome authority and recorded user process
  direction.

### 2. Next steps

1. Delegate TK1-RP2 to the bounded implementation worker.
2. Add the full adversarial suite and preserve review-repair RED.
3. Close the four findings within three GREEN cycles and run every gate.
4. Obtain a fresh distinct deep ACCEPT review before any staging.
5. If a material boundary changes, stop and ask the user; otherwise keep the
   internal correctness loop out of the user's decision path.

### 3. Approved decisions

- TK-R1/TK-A02 and TK-R2/TK-A03 remain active; no new technical decision is
  introduced.
- Active packet: TK1-RP2; scope is the still-unmet portion of the exact TK-A03
  closure outcome.
- Architecture, allowlist, nine commits, original TK1 message, non-force push,
  no-PR rule, and installed-environment policy are unchanged.

### 4. Execution discipline

- Capability profile: native-plan / spawn-send-wait / repo-artifact /
  native-session-poll; repository artifact remains authoritative.
- Only the two implementer files and controller artifact may change.
- Review closure is test-first, bounded, independently reviewed, and cannot be
  staged until all P0/P1 and required P2 findings close.
- Never hide evidence, alter environment/dependencies, execute CAD/network/
  model actions, rewrite history, or infer authority beyond TK-A03.

## TK1-RP2 Circuit-Breaker Evidence — TK1-E003

| Entry ID | Decision / approval | Commit / push | Gate evidence | Residual | Snapshot | State |
|---|---|---|---|---|---|---|
| TK1-E003 / 2026-07-17 | TK-D25–TK-D32 at TK-R2; TK-A03 | not-created / not-pushed | review-repair RED: 38 passed/4 failed; GREEN 1: 42 passed but before the mandatory full transition matrix existed; GREEN 2: failed on a test-helper call; GREEN 3: 42 passed/1 failed on missing needs_input payload in the matrix; earlier cumulative 431 passed/1 deselected and static/import gates predate the final matrix and are not final evidence | TK1-B04 and process-ordering closure remain open; final review not run | TK-S006 | frozen-audit |

The implementer stopped without a fourth run when GREEN cycle 3 failed. The
current hashes are:

- src/vibecad/workflow/state.py:
  337b919106e70bb635d18603c58022449e647a01bd41a1fe06d4ec4d530a9e8f
- tests/test_task_state.py:
  3710c9e2eb97d370eaa0ba899beecc15e4492160ee8d0687c2c489b5f73b2f86

The failure itself is a test-payload defect: ROLLING_BACK + REQUEST_INPUT is a
legal edge, but the exhaustive-matrix helper reused a StepError with
needs_input=false. The implementation correctly rejected the resulting
NEEDS_INPUT state at `/last_error/needs_input`.

There is also an execution-order deviation: the full 13-status × 16-event
matrix was added only after GREEN cycle 1 and the cumulative/static gates.
Consequently the initial review-repair RED and the earlier downstream gates do
not prove the final required test surface. Their outputs remain historical
evidence but cannot authorize staging.

No source/test edit, fourth test, final review, stage, commit, push, install,
CAD, network, or out-of-scope action followed the breaker. The controller now
permits read-only inspection only, to consolidate the entire frozen gap before
issuing any further internal closure packet under the same TK-A03 outcome.

## Recovery Snapshot — TK-S006

### 1. Completed milestones

- TK1-RP2 closed the known implementation defects in immutable verifier data,
  candidate/report provenance, and safe nested error rendering at the current
  source hash.
- The expanded suite exposed a fixture defect after the declared GREEN budget;
  the worker stopped correctly and preserved all evidence.
- No Stage C commit/push or prohibited action exists.
- The exact branch, anchor, three dirty paths, and current source/test hashes
  are known.

### 2. Next steps

1. Perform one fresh, bounded, read-only audit of the frozen source and tests
   against TK-D25–TK-D32, TK1-RP2, and both independent-review reports.
2. Consolidate every remaining implementation/test gap before authorizing any
   further edit or command.
3. If all gaps remain inside the already approved B01–B10 outcome, issue one
   internal test-first closure packet without interrupting the user.
4. If the audit discovers a material product/architecture/security/data/cost/
   scope change, stop and request user direction.

### 3. Approved decisions

- TK-R1/TK-A02 and TK-R2/TK-A03 remain active.
- No new technical outcome is approved or required at this snapshot.
- The user's process direction continues to keep internal correctness-loop
  mechanics out of the user decision path.
- Architecture, allowlist, nine commits, TK1 message, non-force push, and no-PR
  policy remain unchanged.

### 4. Execution discipline

- Capability profile: native-plan / spawn-send-wait / repo-artifact /
  native-session-poll; repository artifact remains authoritative.
- Frozen actions: production/test edits, tests, formatting, review acceptance,
  stage, commit, push, install, CAD, network/model access, and TK2.
- Allowed actions: controller artifact ledger append and bounded read-only
  source/test/spec inspection.
- Preserve the current files and evidence; never reset/discard/rewrite or infer
  that the incomplete final matrix passed.

## TK1 Frozen Audit Evidence — TK1-E004

The fresh frozen audit returned REJECT with P0=0, P1=6, and P2=2. It verified
the TK1-E003 source/test hashes and performed no edit, test/format/CAD run,
install, network action, stage, commit, push, or Git mutation.

All findings remain inside the approved B01–B10 outcome:

- P1 / B03+B05+B08 — JSON node counting excludes scalar leaves; a flat scalar
  container is unbounded. Measurement validation can call math.isfinite on an
  unsafe integer before its safe-integer check and leak OverflowError.
- P1 / B02 — candidate-origin continuation can replace candidate identity;
  ROLLING_BACK/error provenance and report-before-VERIFYING provenance remain
  forgeable.
- P1 / B03+B05 — direct tuple normalization accepts subclasses, and raw nested
  C1 mappings/containers are not guarded as exact built-in types before their
  parsers run.
- P1 / B04 — the full matrix has the known REQUEST_INPUT fixture defect plus
  two latent CONFIRM_COMMITTED fixture defects, and illegal-edge assertions do
  not prove exact code/path/terminal precedence or payload guards.
- P1 / B03+B04 — exact-max/max+1, fail-before-sentinel, duplicates, sequence,
  capacity precedence, and append immutability tests are missing for most of
  the six budgets.
- P1 / B02+B04+B08 — rich JSON/numeric/truth-table/round-trip, continuation,
  stale/missing error, report phase, AcceptanceSpec, artifact, reconciliation,
  and export negative tests remain incomplete; the existing COMMITTING forgery
  test fails at illegal history before reaching its intended invariant.
- P2 / B05 — TaskStateError now quotes output but does not bound path length;
  hostile unknown keys can still create an unbounded rendered exception.
- P2 / B04+B06 — the test file retains a file-wide E501 suppression, and every
  final cumulative/static/import/review gate must be rerun after the final
  matrix actually passes.

No audit finding changes product behavior, architecture, public API, security
or data policy, dependency, cost, external action, allowlist, commit sequence,
or publication authority. Review closure therefore continues internally under
the existing TK-A03 outcome and the user's recorded process direction.

## Task Packet TK1-RP3 — Active Consolidated Closure Under TK-A03

### 1. Authorization

TK-A03 remains the controlling outcome authorization. This packet closes only
TK1-E004 findings mapped to already approved TK1-B02/B03/B04/B05/B06/B08. It
does not add a new technical outcome or expand any product, architecture,
security/data, dependency, environment, external-action, file, commit, push,
or publication boundary.

### 2. Workspace anchor

- Repository: /Users/wangtao/Documents/DevProject/vibecad.
- Branch: codex/task-kernel-phase2.
- Committed anchor: bf8967077addfbd8002d6289b0eb925f3f55b638.
- Frozen source:
  337b919106e70bb635d18603c58022449e647a01bd41a1fe06d4ec4d530a9e8f.
- Frozen tests:
  3710c9e2eb97d370eaa0ba899beecc15e4492160ee8d0687c2c489b5f73b2f86.
- Implementer may modify only tests/test_task_state.py during the test-design
  phase; after controller test-design acceptance, it may also modify
  src/vibecad/workflow/state.py. Controller alone edits this artifact.

### 3. Context

The previous packet mixed test-design completion with GREEN cycles. TK1-RP3
separates test completeness, RED, implementation, and acceptance so no passing
command can precede the required test surface.

Implementation closure must:

- count every JSON scalar and container node before returning; enforce finite
  safe integers, depth, node, cycle, exact-container, alias-free freeze/thaw;
- validate measurement type and safe integer before math.isfinite, use the
  scalar field path for scalar failures and indexed paths for vectors, accept
  only bounded finite numeric vectors, and require nonnegative tolerance;
- preserve an existing candidate revision on candidate-origin continuation and
  reject a different supplied revision;
- require/preserve structured errors in ROLLING_BACK, NEEDS_INPUT,
  RECOVERY_REQUIRED, CLEANUP_REQUIRED, FAILED, and reconciled success while
  rejecting forged errors in ordinary phases and normal COMMIT success;
- reject reports unless history reached VERIFYING, and require a durable
  current-candidate/AcceptanceSpec passing report in COMMITTING/SUCCEEDED;
- require exact built-in dict/list/tuple at every state and nested C1 boundary
  before iteration, with size checks before nested construction;
- bound error paths and field/pointer tokens, JSON-quote rendering, safely
  clamp/reject over-budget nested paths, and preserve exact indexed prefixes
  when within budget;
- reject artifact traversal/path-like names including dot/dot-dot and preserve
  candidate, duplicate, capacity, and immutability rules;
- remove every file-wide lint suppression.

### 4. Steps and gates

1. A fresh implementation worker edits tests/test_task_state.py only. It must
   complete, without running tests:
   - the 13×16 matrix with valid fixtures for every legal edge, exact terminal
     versus invalid-transition code/path assertions, and missing/wrong/extra
     payload guards;
   - exact-max/max+1 plus fail-before-nested-sentinel proof for steps 64,
     transitions 128, reports 16, artifacts 128, verdicts 128, and evidence 32;
   - real duplicate report/artifact/verdict IDs, step/transition sequence
     gaps/duplicates, append capacity precedence, immutability, and round trips;
   - JSON scalar/container node, depth, cycle, repeated-alias, non-JSON,
     non-finite, unsafe-integer, exact-subclass, thawed-type, measurement vector,
     and tolerance boundary cases;
   - required/optional pass/fail/unsupported truth table;
   - candidate continuation identity, StepResult/report/artifact/AcceptanceSpec
     binding, COMMITTING/report phase, stale/missing error provenance, negative
     artifact/reconciliation, hostile key/path/rendering, nested C1 prefix, and
     public export cases.
2. The worker stops and reports the test-only hash without running any command.
   A distinct deep read-only test-design reviewer compares the file against
   TK1-E004 and this packet. Test-design ACCEPT requires P0=0/P1=0 and all P2
   closed. At most two test-design review cycles are allowed; these involve no
   test execution.
3. Only after controller records test-design ACCEPT, run exactly one focused
   repair RED. It must collect normally and fail solely on TK1-E004
   implementation gaps. Unexpected/setup/import failures stop the packet.
4. Implement the minimum two-file closure described above. The implementation
   verification budget is at most four focused GREEN cycles after RED. A failed
   fourth cycle freezes TK1. Preserve every command/count/status.
5. On focused GREEN, run the cumulative TK1/C1–C5 suite, fresh pure-import and
   forbidden-import checks, Ruff check, Ruff format, git diff --check, exact
   dirty-path/allowlist inspection, and mutation/round-trip readback.
6. Assign a new distinct deep read-only acceptance reviewer. ACCEPT requires
   P0=0/P1=0 and every P2 closed or explicitly dispositioned, with explicit
   readback of prior TK1-E002 and TK1-E004 findings.
7. Only after ACCEPT may the controller append TK1 acceptance evidence, stage
   the exact three named files, rerun staged gates, commit the original TK1
   message, push non-force, and verify clean HEAD/upstream equality before TK2.

### 5. Execution discipline

- Delegation: spawn-send-wait; a fresh standard implementer, distinct deep
  test-design reviewer, and distinct deep final reviewer.
- Process: native-session-poll for live sessions; never duplicate a command.
- Test-design phase forbids all test/format commands and source edits.
- Stop on test-design rejection after cycle two, unexpected RED, failed fourth
  GREEN, final P0/P1, unresolved P2, native exception leakage, material-boundary
  change, out-of-allowlist write, process ambiguity, or extra semantic commit.
- Never reset/discard/amend/force-push, hide evidence, alter dependencies or
  environment, execute CAD/network/model actions, stage, commit, push, or start
  TK2 before acceptance.

### 6. Delivery boundary

Deliver only the consolidated TK1 test design, pure state implementation
closure, and declared gates. Controller owns the artifact, review disposition,
staging, commit, push, and TK2 activation. No public MCP or user-facing feature
is introduced.

### 7. Final report

Return test-design hashes and review outcomes; repair RED; every GREEN command,
count, and status; cumulative/static/import evidence; immutable JSON, bounded
error, and provenance design summary; finding-by-finding closure; deviations
and residuals; prohibited-action confirmation; and final branch/HEAD/status.

## Recovery Snapshot — TK-S007

### 1. Completed milestones

- TK1-E004 consolidated every frozen implementation/test gap into one list.
- No finding crosses the approved TK-A03 B01–B10 outcome.
- TK1-RP3 is active and separates test design from test execution.
- No Stage C commit/push or prohibited action exists.

### 2. Next steps

1. Fresh worker completes tests only and does not run them.
2. Distinct deep reviewer accepts the static test design before RED.
3. Worker runs one RED, repairs within four GREEN cycles, and completes gates.
4. Fresh distinct deep reviewer returns final ACCEPT before staging.

### 3. Approved decisions

- TK-R1/TK-A02 and TK-R2/TK-A03 remain active.
- Active packet: TK1-RP3 under the same approved closure outcome.
- Architecture, allowlist, nine commits, original TK1 message, non-force push,
  no-PR boundary, and installed-environment policy remain unchanged.

### 4. Execution discipline

- Capability profile: native-plan / spawn-send-wait / repo-artifact /
  native-session-poll; repository artifact remains authoritative.
- Test design must be accepted before any new command or source edit.
- All work remains test-first, bounded, independently reviewed, and unstaged
  until final acceptance.
- Preserve evidence/user work and never infer authority beyond TK-A03.

## TK1-RP3 Static Test-Design Review 1 — TK1-E005

The first static test-design review returned REJECT with P0=0, P1=7, and
P2=1 against test hash
e6a6a5143ba28513e967e2f8f3d8247a8b8fbd9cbb954de7f1594fdba607d90.
Source remained frozen at
337b919106e70bb635d18603c58022449e647a01bd41a1fe06d4ec4d530a9e8f.

The review found:

- the older full-matrix test still retains the known REQUEST_INPUT fixture
  defect even though the new matrix fixtures are correct;
- required identifier and missing/wrong/extra payload guards are incomplete;
- budget tests use implementation constants as the sole oracle, do not prove a
  valid exact 128-transition boundary, and incompletely cover both sequence
  histories plus append precedence/immutability;
- exact-container tests omit direct TaskRun collections and most nested C1
  mapping boundaries/hostile subclasses;
- verdict contradiction, rich round-trip, numeric vector length, scalar
  non-finite, and broader measurement boundaries are missing;
- reachable COMMITTING/SUCCEEDED, candidate binding, error provenance, and
  resubmission-clear tests remain incomplete;
- artifact phase and reconciliation evidence/payload negatives remain
  incomplete;
- public export coverage proves only a positive subset and not the complete
  surface or deferred Intent exclusion.

The reviewer also confirmed that the new corrected 13×16 matrix fixtures, all
six max+1 sentinel parser cases, major JSON bounds/alias/cycle/thaw tests,
bounded rendering tests, and removal of file-wide lint suppression are present.

No executable validation or mutation occurred. TK1-RP3 permits one final
test-design repair/review cycle. Tests and source execution remain forbidden;
the worker may edit only tests/test_task_state.py to close every item above,
then a distinct static reviewer must return ACCEPT before RED.

## TK1-RP3 Static Test-Design Review 2 — TK1-E006

The final TK1-RP3 static test-design review returned REJECT with P0=0, P1=7,
and P2=1 against test hash
fe4c9e384ede2a29d34015655593758850dd5bcb8b8e2f51d7be30de7bf8efc1.
Source remained frozen at
337b919106e70bb635d18603c58022449e647a01bd41a1fe06d4ec4d530a9e8f.

The reviewer confirmed the corrected new 13×16 matrix, literal six-budget
values, max+1 sentinels, valid 128-transition history, major rich JSON tests,
artifact phase checks, exact __all__, and lint-suppression removal. Remaining
test-design blockers are:

- the independently collected old matrix still retains its REQUEST_INPUT
  false-needs-input fixture;
- wrong/extra payload guards and identifier precedence are incomplete across
  all event and candidate-bound mapping families;
- transition gap versus duplicate proof and accepted-list defensive copying are
  incomplete;
- a nested-hostile fixture reuses a prior transitions ListSubclass and fails at
  the wrong path;
- direct verdict/evidence and nested C1 hostile-container boundaries do not
  prove rejection before hostile iteration;
- tolerance lacks its own vector-capacity and unsafe-integer cases;
- durable raw candidate/AcceptanceSpec forgeries, present-nonpassing recovery,
  and cleanup-origin error preservation are incomplete;
- Intent exclusion checks only __all__, not TaskRun fields/mapping rejection.

The reviewer made no edit or executable/prohibited action. TK1-RP3's two static
design cycles are exhausted. This is still exclusively test correctness inside
approved TK1-B02/B03/B04/B05/B08 and presents no user-level decision.

## Task Packet TK1-RP4 — Active Controller Test-Design Closure Under TK-A03

### 1. Authorization

TK-A03 remains the outcome authority. TK1-RP4 changes no production contract,
source, architecture, product behavior, external action, file allowlist,
commit, or publication policy. It permits the controller to close only the
TK1-E006 test-design findings before any RED or source edit.

### 2. Workspace anchor

- Repository: /Users/wangtao/Documents/DevProject/vibecad.
- Branch/HEAD:
  codex/task-kernel-phase2@bf8967077addfbd8002d6289b0eb925f3f55b638.
- Source must remain exactly
  337b919106e70bb635d18603c58022449e647a01bd41a1fe06d4ec4d530a9e8f.
- Starting tests:
  fe4c9e384ede2a29d34015655593758850dd5bcb8b8e2f51d7be30de7bf8efc1.
- Controller may edit only tests/test_task_state.py plus this artifact.

### 3. Context

The implementation worker twice reported static completeness while leaving
review-detectable fixture and coverage gaps. The controller now owns targeted
test repair using the exact TK1-E006 evidence. Source and executable validation
remain frozen so the next RED has one trustworthy, complete oracle.

### 4. Steps and gates

1. Patch only tests/test_task_state.py to close every TK1-E006 bullet exactly.
   Remove or correct the old REQUEST_INPUT matrix; reset independent raw
   fixtures; add complete wrong/extra payload and identifier mapping cases;
   separate transition gap/duplicate tests; prove list defensive copies;
   add hostile direct/nested containers that raise if iterated; add tolerance
   boundaries; add durable raw provenance/recovery/error cases; and prove
   TaskRun has no Intent field/key and rejects an intent mapping member.
2. Do not run pytest, Python, Ruff, formatter, CAD, network, install, or source
   code. Compute/read hashes and inspect text only.
3. Return the patched test hash to the same static deep reviewer for focused
   TK1-E006 closure plus regression against the full TK1-RP3 surface.
4. Up to three controller static repair/review cycles are permitted without
   executable validation. ACCEPT requires P0=0/P1=0/P2=0.
5. Only after static ACCEPT may the controller record the accepted test hash
   and authorize the single repair RED and source implementation phase.

### 5. Execution discipline

- Persistence remains repo-artifact; native plan is a projection.
- Source, tests execution, formatter, staging, commit, push, environment, CAD,
  model, and network actions remain frozen.
- Stop if test repair requires a production-contract or material-boundary
  change; otherwise keep internal test correctness out of the user path.
- Never reset/discard/rewrite or conceal prior test-design rejections.

### 6. Delivery boundary

Deliver only a statically accepted tests/test_task_state.py design. Production
repair, RED/GREEN, final review, staging, commit, push, and TK2 remain outside
this packet.

### 7. Final report

Record each TK1-E006 closure with test lines, hashes before/after, every static
review verdict, confirmation source remained frozen, and confirmation that no
executable/prohibited action occurred.

## Recovery Snapshot — TK-S008

### 1. Completed milestones

- Two TK1-RP3 static test-design reviews preserved exact remaining gaps.
- No RED or source edit occurred against an incomplete design.
- No Stage C commit/push or prohibited action exists.
- TK1-RP4 is active for controller-owned targeted test repair only.

### 2. Next steps

1. Controller patches the eight TK1-E006 categories in tests only.
2. Static reviewer returns ACCEPT with zero findings.
3. Record accepted hash, then issue the already planned repair RED/source phase.

### 3. Approved decisions

- TK-R1/TK-A02 and TK-R2/TK-A03 remain active.
- No new technical outcome exists; TK1-RP4 closes the same B01–B10 tests.
- Architecture, allowlist, nine commits, TK1 message, push/no-PR policy, and
  environment policy are unchanged.

### 4. Execution discipline

- Capability profile remains native-plan / spawn-send-wait / repo-artifact /
  native-session-poll.
- Only controller test/artifact edits and static read-only review are allowed.
- Preserve all source and execution state until static test ACCEPT.
- Never infer external authority or run a test early.

## TK1-RP4 Static Test-Design Review 1 — TK1-E007

The first controller-owned static review returned REJECT with P0=0, P1=3,
and P2=0 against test hash
edecaa1284757d50b395be6f46b6d097fa62dd5006d92fb3488ea07180256c76.
Source remained frozen at
337b919106e70bb635d18603c58022449e647a01bd41a1fe06d4ec4d530a9e8f.

The reviewer confirmed every TK1-E006 category except three test-only gaps:

- the hostile nested-step fixture still reused a mapping whose transitions
  collection had been replaced by a hostile list subclass;
- missing and wrong program coverage existed for initial submission but not
  for candidate-origin NEEDS_INPUT resubmission;
- defensive-copy coverage existed for the four TaskRun collections but not
  for VerificationReport.verdicts and CriterionVerdict.evidence.

No source, executable validation, formatter, CAD, network, install, Git, or
publication action occurred. All three findings remain internal correctness
work inside TK-A03 and require no user-level choice.

## TK1-RP4 Controller Static Repair Cycle 2 — TK1-E008

The controller patched tests/test_task_state.py only:

- reset the TaskRun mapping immediately before the independent hostile
  /steps/0 fixture;
- added missing and wrong program cases for NEEDS_INPUT + SUBMIT_PROGRAM;
- added accepted builtin-list mutation checks for verdict evidence and report
  verdicts, completing all six declared bounded sequence inputs.

Starting artifact, test, and frozen source hashes were respectively
e23dae11600cee040d128e3c2457a57cfaf29b7e66989573bc3a82cb4301e47e,
edecaa1284757d50b395be6f46b6d097fa62dd5006d92fb3488ea07180256c76,
and 337b919106e70bb635d18603c58022449e647a01bd41a1fe06d4ec4d530a9e8f.
The static reviewer must now return P0=0/P1=0/P2=0 before any RED. No test,
Python, Ruff, formatter, source edit, CAD, network, install, stage, commit,
push, or publication action is authorized in this cycle.

## TK1-RP4 Static Test-Design Acceptance — TK1-E009

The distinct deep reviewer returned ACCEPT with P0=0, P1=0, and P2=0 against
test hash 5507c54593e432a0f53e306f471e40d4edc02d3e0dbe850919a297e22d31e301,
2742 lines. Branch/HEAD remained
codex/task-kernel-phase2@bf8967077addfbd8002d6289b0eb925f3f55b638 and
source remained frozen at
337b919106e70bb635d18603c58022449e647a01bd41a1fe06d4ec4d530a9e8f.

The reviewer verified the independent hostile fixture reset, both
NEEDS_INPUT + SUBMIT_PROGRAM required-program guards, all six bounded sequence
defensive-copy inputs, and the complete TK1-E006/TK1-RP3 surface. No static
syntax, name, import, or fixture blocker remains. No executable validation,
source edit, formatter, CAD, network, install, or Git mutation occurred.

## Task Packet TK1-RP5 — Active Accepted-Oracle Implementation Closure Under TK-A03

### 1. Authorization

TK-A03 remains the controlling outcome authorization. TK1-RP5 executes the
already planned RED, production repair, and verification portions of TK1-RP3
against the accepted TK1-E009 test oracle. It closes only TK1-B01–TK1-B10 and
TK-D25–TK-D32. It changes no product outcome, architecture, dependency,
environment, external action, commit, push, or publication boundary.

### 2. Workspace anchor

- Repository: /Users/wangtao/Documents/DevProject/vibecad.
- Branch/HEAD:
  codex/task-kernel-phase2@bf8967077addfbd8002d6289b0eb925f3f55b638.
- Accepted immutable test oracle:
  5507c54593e432a0f53e306f471e40d4edc02d3e0dbe850919a297e22d31e301.
- Starting production source:
  337b919106e70bb635d18603c58022449e647a01bd41a1fe06d4ec4d530a9e8f.
- Worker may edit only src/vibecad/workflow/state.py. Tests and this artifact
  are controller-owned and frozen during implementation.

### 3. Context

The accepted oracle covers exact JSON and numeric budgets, strict container
boundaries, bounded safe errors, legal transition payloads, candidate/report/
artifact/AcceptanceSpec provenance, recovery and cleanup evidence, durable
error history, immutable sequence inputs, and Intent deferral. The source is
known to predate parts of that oracle; the RED must distinguish those expected
implementation gaps from setup, collection, import, or fixture defects.

### 4. Steps and gates

1. Reconfirm branch/HEAD, the three dirty paths, accepted test hash, starting
   source hash, and artifact hash without mutation.
2. Run exactly one focused repair RED:

       PYTHONPATH=src .venv/bin/pytest -q tests/test_task_state.py

   It must collect normally and fail only on approved TK1-B01–TK1-B10 gaps.
   Setup, collection, import, native-exception, or unrelated failures stop the
   packet without a source edit.
3. Edit only src/vibecad/workflow/state.py for the minimum TK1-RP3 closure.
   Do not modify the accepted tests or add persistence, filesystem, CAD, MCP,
   model, clock, random-ID, dependency, environment, or network behavior.
4. Run the same focused command after each bounded repair. At most four focused
   GREEN cycles are available after RED; a failed fourth cycle freezes TK1.
   Preserve every exact command, exit code, pass/fail count, and signature.
5. On focused GREEN, run the cumulative TK1/C1–C5 command recorded in TK1-RP1,
   then pure-import and forbidden-import checks, Ruff check, Ruff format check,
   git diff --check, exact dirty-path/allowlist inspection, and the accepted
   mutation/round-trip readback tests.
6. Return hashes and evidence to the controller. A new distinct deep read-only
   reviewer must replay TK1-E002, TK1-E004, and TK1-E009 and return
   P0=0/P1=0/P2=0 before acceptance.
7. Only after final ACCEPT may the controller append acceptance evidence,
   stage the exact three files, rerun staged gates, create the original TK1
   commit, push non-force, verify HEAD/upstream equality, and activate TK2.

### 5. Execution discipline

- Delegation: spawn-send-wait with a fresh implementation worker and a later
  distinct deep acceptance reviewer.
- Process: native-session-poll; never relaunch a live process.
- Tests and artifact remain frozen; production writes are restricted to the
  one named source file.
- Stop on unexpected RED, failed fourth GREEN, accepted-test hash drift,
  out-of-allowlist write, native exception leakage, material-boundary change,
  process ambiguity, or any need for an additional semantic commit.
- Never reset/discard/amend/force-push, conceal evidence, install or alter the
  environment, execute CAD/network/model actions, stage, commit, push, or start
  TK2 before final acceptance.

### 6. Delivery boundary

Deliver only the pure TK1 state-contract repair and declared local evidence.
No public MCP, CAD execution, persistence, lease, service, or user-facing
feature is introduced. Controller retains artifact, review, Git, and TK2
authority.

### 7. Final report

Return the preflight hashes; focused RED command/status/count/signatures and
their blocker mapping; each GREEN command/status/count; source hash after every
repair; cumulative/static/import/readback gates; exact dirty paths; residuals;
prohibited-action confirmation; and branch/HEAD/status.

## Recovery Snapshot — TK-S009

### 1. Completed milestones

- TK1-RP4 static test design is ACCEPT with P0=0/P1=0/P2=0.
- The accepted oracle hash is fixed and production source is still frozen.
- No RED, source repair, stage, commit, push, or TK2 action occurred yet.

### 2. Next steps

1. Fresh implementation worker runs the single focused RED.
2. If RED is expected, repair only state.py within four GREEN cycles.
3. Run cumulative/static/import gates and obtain a distinct final ACCEPT.

### 3. Approved decisions

- TK-R1/TK-A02 and TK-R2/TK-A03 remain active.
- TK1-RP5 is internal execution of the already approved B01–B10 outcome.
- No additional user approval is required unless a material boundary changes.

### 4. Execution discipline

- Preserve accepted tests at their exact hash throughout implementation.
- Preserve every RED/GREEN/review result, including failures.
- No environment/CAD/network/model/Git publication action is authorized before
  final acceptance.

## TK1-RP5 RED Circuit-Breaker Evidence — TK1-E010

The fresh worker completed the one authorized focused RED without changing any
file:

    PYTHONPATH=src .venv/bin/pytest -q tests/test_task_state.py

It collected 362 tests normally, exited 1 after 33.80 seconds, and reported
343 passed and 19 failed. Branch/HEAD, the three dirty paths, accepted tests
5507c54593e432a0f53e306f471e40d4edc02d3e0dbe850919a297e22d31e301,
source 337b919106e70bb635d18603c58022449e647a01bd41a1fe06d4ec4d530a9e8f,
and artifact c2a62c45d3d17338ef4bd81d89297480c3c2429a41dab9c4d374a5ae191fd9fb
all matched before execution and remained unchanged afterward.

The 19 failures were all within the accepted B01–B10 oracle:

1. Three failure-payload tests received MISSING_ERROR instead of INVALID_TYPE
   for a present wrong-type error.
2. JSON node accounting omitted scalar leaves.
3. One nested non-JSON value reported /expected/bad instead of root /expected.
4. Unsafe integer delta leaked OverflowError before safe-integer validation.
5. A boolean delta returned INVALID_TYPE instead of INVALID_VALUE.
6. Negative scalar tolerance reported /tolerance/0 instead of /tolerance.
7. A nested program DictSubclass was accepted.
8. Candidate-origin continuation accepted a changed candidate revision.
9. Artifact name `.` was accepted.
10. Artifact name `..` was accepted.
11. A hostile mapping key produced a 10001-character unbounded error path.
12. A direct TupleSubclass was accepted.
13. A scalar NaN delta reported /delta/0 instead of /delta.
14. An ordinary durable state accepted a forged last_error.
15. Missing candidate_revision in a candidate-bound mapping returned
    MISSING_FIELD instead of INVALID_IDENTIFIER.
16. A hostile tuple subclass was iterated and leaked AssertionError.
17. Unsafe integer tolerance leaked OverflowError.
18. Exact nested-container, provenance, and error-history failures caused the
    first assertion in their aggregate tests to stop later cases.
19. The aggregate numeric/container failures likewise stopped their remaining
    accepted subcases before execution.

TK1-RP5 required any native exception during RED to stop the packet. The two
native signatures were `OverflowError: int too large to convert to float` and
`AssertionError: hostile tuple was iterated`. The worker therefore correctly
stopped before a source edit or GREEN. However, TK1-RP3 and the accepted oracle
explicitly define safe-integer-before-finite validation and reject-before-
iteration exact containers as production gaps. The stop wording was too broad:
these two native exceptions are expected RED evidence, not an unrelated setup
or harness failure. Correcting that internal classification changes no product
contract, security/data boundary, external action, cost, file scope, or user
choice. No additional user approval is required under TK-A03.

## Task Packet TK1-RP6 — Active Known-RED Source Closure Under TK-A03

### 1. Authorization

TK-A03 authorizes the B01–B10 outcome; TK1-E009 supplies the accepted oracle
and TK1-E010 supplies its genuine RED. TK1-RP6 permits the minimum source repair
for all 19 observed failures, including conversion of the two known native
leaks into stable TaskStateError results. It supersedes TK1-RP5 only by
classifying those exact two signatures as expected implementation gaps. No
other native exception is authorized or suppressible.

### 2. Workspace anchor

- Repository: /Users/wangtao/Documents/DevProject/vibecad.
- Branch/HEAD:
  codex/task-kernel-phase2@bf8967077addfbd8002d6289b0eb925f3f55b638.
- Accepted tests:
  5507c54593e432a0f53e306f471e40d4edc02d3e0dbe850919a297e22d31e301.
- Starting source:
  337b919106e70bb635d18603c58022449e647a01bd41a1fe06d4ec4d530a9e8f.
- Genuine RED is TK1-E010 and must not be repeated.
- Worker may edit only src/vibecad/workflow/state.py; tests and artifact are
  frozen and controller-owned.

### 3. Context

The RED proves strict validation-order, budget, pointer, exact-container,
candidate-provenance, durable-error, artifact-name, and required-identifier
gaps. Aggregate tests may reveal later assertions only after earlier source
defects close, but every assertion is already part of the accepted immutable
oracle. Source repair must preserve normal canonical pointers and valid
round-trips while failing before hostile iteration or unsafe numeric calls.

### 4. Steps and gates

1. Reconfirm branch/HEAD, three dirty paths, frozen test/source hashes, and
   artifact hash. Do not rerun RED.
2. Edit only state.py to close TK1-E010 and all later assertions already present
   in the accepted aggregate tests. Use exact builtin container checks before
   copying/iteration; count every JSON node; validate numeric type and safe
   integer before finite checks; preserve scalar versus indexed paths; bound
   hostile pointer construction/rendering; enforce candidate, report,
   artifact, AcceptanceSpec, reconciliation, and last_error provenance; and
   establish required candidate identifier precedence.
3. Run the focused command after each bounded repair. The first run is GREEN
   cycle 1, not another RED. At most four focused GREEN cycles are available;
   the failed fourth cycle freezes TK1. Any native exception other than the two
   exact TK1-E010 signatures during the first cycle stops the packet. Neither
   known signature may remain in a passing result or be hidden/caught broadly.
4. On focused GREEN, run the exact cumulative TK1/C1–C5 command from TK1-RP1,
   fresh pure-import and forbidden-import checks, Ruff check, Ruff format check,
   git diff --check, exact dirty-path/allowlist inspection, and mutation/
   round-trip readback evidence.
5. Reconfirm the accepted test and artifact hashes did not change and report
   the source hash after every repair cycle.
6. Return all evidence to the controller. A new distinct deep read-only final
   reviewer must replay TK1-E002, TK1-E004, TK1-E009, and TK1-E010 and return
   P0=0/P1=0/P2=0.
7. Only after final ACCEPT may the controller append acceptance evidence,
   stage the exact three files, rerun staged gates, make the original TK1
   commit, push non-force, verify HEAD/upstream equality, and activate TK2.

### 5. Execution discipline

- Delegation: a fresh implementation worker followed by a distinct deep
  read-only final reviewer; spawn-send-wait and native-session-poll.
- Four focused GREEN cycles remain; TK1-RP5 consumed only the RED.
- Do not edit tests/artifact, rerun RED, install or change environment, add
  dependencies, use CAD/network/model tools, stage, commit, push, or start TK2.
- Stop on unexpected native exception, fourth failed GREEN, frozen-hash drift,
  out-of-allowlist write, material-boundary change, process ambiguity, or any
  need for another semantic commit.
- Never reset/discard/amend/force-push or conceal any failed assertion.

### 6. Delivery boundary

Deliver only the pure state.py closure and declared local verification. No new
service, persistence, lease, MCP, CAD execution, user-facing feature, external
side effect, or publication is introduced.

### 7. Final report

Return preflight evidence; source changes by function; every GREEN command,
exit/count/signature and source hash; cumulative/import/static/readback gates;
proof tests/artifact stayed frozen; dirty paths; remaining risks or residuals;
prohibited-action confirmation; and branch/HEAD/status.

## Recovery Snapshot — TK-S010

### 1. Completed milestones

- TK1-E009 accepted the full static oracle.
- TK1-E010 preserved one genuine RED: 343 passed, 19 failed.
- No source/test/artifact mutation, GREEN, Git publication, or TK2 action
  occurred during TK1-RP5.

### 2. Next steps

1. Fresh TK1-RP6 worker repairs only state.py without repeating RED.
2. Reach focused and cumulative/static/import GREEN within four cycles.
3. Obtain distinct final deep ACCEPT before any Git action.

### 3. Approved decisions

- TK-R1/TK-A02 and TK-R2/TK-A03 remain active.
- The two TK1-E010 native leaks are expected source gaps under TK1-B03/B05/B08.
- This classification repair is internal and requires no user decision.

### 4. Execution discipline

- Accepted tests and artifact are frozen throughout worker execution.
- Preserve the genuine RED; do not rerun or reinterpret it as success.
- No external/environment/CAD/model/Git action before final acceptance.

## TK1-RP6 Implementation Evidence — TK1-E011

The fresh worker did not repeat TK1-E010 RED and edited only state.py. Starting
hashes and branch/HEAD matched TK1-RP6. GREEN cycle 1 used the exact focused
command and exited 0 with 362 passed in 7.89 seconds at source hash
d007dc6f75cdc4fcc71094481b6f4e3a51cd59e4ef8d94affb944089005c1607.
After source-only formatting by apply_patch, GREEN cycle 2 exited 0 with
362 passed in 8.37 seconds at final source hash
8a2d2bf65c015258e455d0e7efb221c5da2b16ff582907c2a3ac179a06e4cd22.
Cycles 3 and 4 were not used; neither native RED signature remained.

The final worker gates were:

- cumulative TK1/C1–C5: 751 passed, 1 deselected in 17.32 seconds;
- mutation/round-trip readback: 6 passed in 0.29 seconds;
- fresh pure import: exit 0, `task state pure import OK`;
- forbidden-import scan: exit 0, empty module list;
- source-only Ruff check: all checks passed;
- source-only Ruff format check: one file already formatted;
- git diff --check: exit 0;
- dirty paths remained exactly artifact, state.py, and test_task_state.py;
- tests remained
  5507c54593e432a0f53e306f471e40d4edc02d3e0dbe850919a297e22d31e301;
- artifact remained
  6669af0dbba751d5da97e4071db71f45224270aa8cb66038d179f907e4605731.

Production closure implements bounded canonical errors, fail-before-iteration
exact containers, complete JSON accounting, safe numeric ordering and paths,
pathless artifact names, identifier precedence, candidate continuation
identity, structured error precedence, report/AcceptanceSpec phase binding,
and durable error/reconciliation provenance.

Combined Ruff evidence is not yet final: test_task_state.py alone reports I001
at line 3 and would be reformatted. The worker preserved the accepted test hash
as required and did not claim ACCEPT. This is a mechanical frozen-test gate,
not a production-contract or user-level decision.

## Task Packet TK1-RP7 — Active Frozen-Test Formatting Migration Under TK-A03

### 1. Authorization

TK-A03 remains the outcome authority. TK1-RP7 permits the controller to migrate
the already accepted TK1-E009 test oracle through only Ruff import ordering and
formatting so the declared two-file static gate can pass. It authorizes no test
assertion, fixture, constant, parametrization, source, contract, dependency,
environment, product, external action, commit, push, or publication change.

### 2. Workspace anchor

- Repository: /Users/wangtao/Documents/DevProject/vibecad.
- Branch/HEAD:
  codex/task-kernel-phase2@bf8967077addfbd8002d6289b0eb925f3f55b638.
- Accepted pre-format test hash:
  5507c54593e432a0f53e306f471e40d4edc02d3e0dbe850919a297e22d31e301.
- Frozen source hash:
  8a2d2bf65c015258e455d0e7efb221c5da2b16ff582907c2a3ac179a06e4cd22.
- Starting artifact hash:
  6669af0dbba751d5da97e4071db71f45224270aa8cb66038d179f907e4605731.
- Controller may edit only tests/test_task_state.py and this artifact; source
  must remain byte-for-byte frozen.

### 3. Context

Functional production and cumulative gates are green. The only recorded static
residual is deterministic I001 import ordering plus Ruff formatting in the
accepted test file. The migration must remain mechanically reviewable and then
re-prove every accepted behavior against the unchanged source.

### 4. Steps and gates

1. Reconfirm anchors and obtain Ruff check/format diffs without mutation.
2. Apply only the exact import-order and formatting changes, preferably through
   apply_patch from those diffs. Do not alter any token with test semantics.
3. Recompute the test hash and inspect the complete formatting delta. Any
   assertion, literal, identifier, decorator, parameter, fixture, or control-
   flow change stops the packet.
4. Run focused test_task_state.py once. It must report 362 passed; any failure
   stops without source repair. Then rerun cumulative TK1/C1–C5 and mutation/
   round-trip readback.
5. Run Ruff check and format check on both source and test, git diff --check,
   pure import, forbidden-import scan, exact dirty-path/allowlist inspection,
   and confirm source/artifact hashes remained frozen during test execution.
6. Assign a distinct deep read-only final reviewer over TK1-E002, TK1-E004,
   TK1-E009, TK1-E010, TK1-E011, the formatted oracle, and source. ACCEPT
   requires P0=0/P1=0/P2=0 and explicit confirmation the formatting migration
   made no semantic test change.
7. Only after ACCEPT may the controller append final evidence, stage the exact
   three files, rerun staged gates, create the original TK1 commit, push
   non-force, verify HEAD/upstream equality, and activate TK2.

### 5. Execution discipline

- Controller owns the mechanical test migration; final review must be distinct.
- One formatting pass and one focused post-format validation are authorized.
- Source is frozen; tests may change only by the recorded Ruff delta.
- Stop on semantic delta, test failure, source/artifact drift, out-of-allowlist
  write, unexpected native exception, material-boundary change, or process
  ambiguity.
- Never reset/discard/amend/force-push, install/change environment, execute
  CAD/network/model actions, stage, commit, push, or start TK2 before ACCEPT.

### 6. Delivery boundary

Deliver only a Ruff-clean, semantically identical test oracle plus repeated
local evidence. No production behavior or user-facing feature changes.

### 7. Final report

Return Ruff pre-format diffs, exact mechanical changes, old/new test hashes,
focused/cumulative/readback/static/import gates, frozen source/artifact proof,
dirty paths, final-review result, prohibited-action confirmation, and
branch/HEAD/status.

## Recovery Snapshot — TK-S011

### 1. Completed milestones

- Source closure reached focused and cumulative GREEN within two cycles.
- Pure import, forbidden import, source Ruff, readback, and diff gates pass.
- Only the frozen test formatting residual remains before final review.

### 2. Next steps

1. Apply the exact Ruff-only test formatting migration.
2. Re-prove focused/cumulative/readback/static/import gates.
3. Obtain distinct final ACCEPT before any Git mutation.

### 3. Approved decisions

- TK-R1/TK-A02 and TK-R2/TK-A03 remain active.
- TK1-RP7 is internal mechanical gate closure, not a new user decision.
- Source hash 8a2d2bf6... is frozen until final review.

### 4. Execution discipline

- No semantic test edit or source edit is authorized.
- Preserve before/after hashes and the complete Ruff delta.
- No external/environment/CAD/model/Git action before ACCEPT.

## TK1-RP15 Minimum Repair and GREEN Evidence — TK1-E035

At 2026-07-18T00:12:44Z the controller matched branch/HEAD
`codex/task-kernel-phase2@bf8967077addfbd8002d6289b0eb925f3f55b638`,
the exact three approved untracked paths, pre-entry artifact
`5db3ec9439dbaebd5e2560e8e448f042d1e093a7610684c62cb1962cdc995609`
at 3558 lines, frozen tests
`fbba54ae92a571fdd18906bcd905db10836a2c85f2b90e0e9593fc5b82a4a0e4`
at 3319 lines, and repaired source
`828dfc407b4568d5300b07ce6b3df7a4024ec9c2959b94c038e8846a25c72ef2`
at 1743 lines.

The minimum source repair adds the existing 256-character ceiling before Enum
conversion. Root unknown-field handling now scans for an overlong key before
sorting; an overlong key uses the existing truncated pointer, while mappings
containing only ordinary keys retain lexicographic selection. No public type,
state, error code, path policy, dependency, or file scope changed.

Objective gates completed normally on the first and only focused GREEN cycle:

- `PYTHONPATH=src .venv/bin/pytest -q tests/test_task_state.py`: exit 0,
  413 passed in 4.92 seconds.
- The declared cumulative TK1/C1-C5 pytest command over six test files: exit 0,
  802 passed and 1 deselected in 4.37 seconds.
- The six declared mutation, round-trip, capacity, rich-JSON, contradiction,
  and direct-list readbacks: exit 0, 6 passed in 0.38 seconds.
- Ruff check: exit 0, all checks passed; Ruff format check: exit 0, both files
  already formatted.
- Fresh pure import: exit 0, `task state pure import OK`; forbidden imported-
  module list was empty.
- Broad `BaseException`/`Exception` and `abs(` search returned no source or test
  matches; `git diff --check` exited 0.
- Dirty and untracked paths remained exactly the artifact, `state.py`, and
  `test_task_state.py`; no second GREEN cycle was needed.

No test, dependency, environment, CAD, network, model, install, stage, commit,
push, TK2, or external action occurred beyond the commands recorded above.
Source and tests now freeze at the hashes above for final review.

## Task Packet TK1-RP16 — Final Complete TK1 Acceptance Review Under TK-A03

### 1. Authorization

TK-R1/TK-A02 and the more specific TK-R2/TK-A03 authorize this distinct final
read-only review of TK1 and closure of TK1-E031 through TK1-E035. This packet
inherits all higher-priority system, developer, and user instructions, any
applicable directory-scoped `AGENTS.md`/`CLAUDE.md`, the approved file
allowlist, and the current host permission model and sandbox. The Skill,
artifact, and packet cannot grant or expand permissions, elevate authority, or
bypass that model or sandbox. Do not request the same approval again.

### 2. Workspace anchor

- Repository: `/Users/wangtao/Documents/DevProject/vibecad`.
- Branch/HEAD:
  `codex/task-kernel-phase2@bf8967077addfbd8002d6289b0eb925f3f55b638`.
- Source: `828dfc407b4568d5300b07ce6b3df7a4024ec9c2959b94c038e8846a25c72ef2`,
  1743 lines.
- Tests: `fbba54ae92a571fdd18906bcd905db10836a2c85f2b90e0e9593fc5b82a4a0e4`,
  3319 lines.
- Controller will supply the post-entry artifact hash and line count with the
  live review assignment.
- Dirty paths must remain exactly the three approved untracked files. No
  applicable VibeCAD-scoped `AGENTS.md` or `CLAUDE.md` was observed.
- Current host permissions and sandbox remain binding. Review is text, hash,
  and status inspection only; no write or external side effect is permitted.

### 3. Context

TK1 defines the durable task-run state contracts and must be fail-closed,
bounded, immutable, provider-neutral, pure on import, and faithful to all
candidate, AcceptanceSpec, report, artifact, error, transition, and recovery
provenance. RP14 found two final work-bound defects. RP15 added six static-
reviewed nodes, produced exactly the predicted three-failure RED, and closed
the two defects on the first GREEN cycle. Passing gates are necessary but not
sufficient for acceptance.

### 4. Steps and gates

1. Reconfirm branch/HEAD, exact hashes and line counts, dirty paths, public
   exports, and absence of unrelated drift using read-only inspection.
2. Re-evaluate `_enum` for invalid exact types, exact 256/max+1 behavior,
   ASCII/multibyte inputs, bounded native-exception work, stable code/path, and
   direct/public parser reachability.
3. Re-evaluate root `_mapping` behavior for required-field precedence, 64/65
   mappings, ordinary unknown-key lexicographic selection, overlong shared-
   prefix keys, truncated paths, and work performed before every bound.
4. Replay every earlier P0/P1/P2 item and TK1-B01-TK1-B10, including complete
   state-machine and provenance matrices, immutable serialization, aliases,
   cycles, depth/nodes, negative integer bounds, public error translation,
   exports, and pure-import separation. Attempt concrete bypasses rather than
   relying on passing tests.
5. Review test oracles for false positives, sentinel reachability/restoration,
   and exact boundary coverage. Confirm RP15 did not weaken any prior behavior.
6. Return ACCEPT only for P0=0/P1=0/P2=0. Otherwise return REJECT with exact
   source/test lines, failure shape, severity, and B01-B10/decision mapping.
7. Confirm no edit, pytest/Python/Ruff/formatter execution, Git mutation,
   CAD/environment/network/model/install action, TK2 work, or external side
   effect occurred.

### 5. Execution discipline

- Delegation: `spawn-send-wait`; model tier: `deep`; process profile:
  `native-session-poll` if any controllable long command existed, though this
  packet permits no executable validation.
- Use a reviewer distinct from the implementation worker. The reviewer may
  perform only read-only text/hash/status inspection and must stop on any hash,
  allowlist, or scope mismatch.
- Any P0/P1/P2 finding is a gate red. Controller owns repair routing and all
  ledger edits; no reviewer mutation or self-disposition is permitted.

### 6. Delivery boundary

Deliver only an evidence-backed finding ledger and ACCEPT/REJECT verdict.
Controller retains acceptance, artifact updates, staging, staged gates,
commit, push, recovery, and TK2 authority.

### 7. Final report

Return exact artifact/source/test hashes and line counts; branch/HEAD/status;
P0/P1/P2 counts; explicit RP14/RP15 closure; full prior-finding and state/
provenance replay; new adversarial analysis; residual risks; prohibited-action
confirmation; and final verdict.

## Recovery Snapshot — TK-S016

### 1. Completed milestones

- TK1-RP15 produced the exact three-failure RED and passed the minimum repair
  on its first focused GREEN cycle.
- Focused, cumulative, six-readback, Ruff, pure-import, broad-exception,
  diff, hash, and allowlist gates are green at the frozen source/test hashes in
  TK1-E035.
- No Git publication or TK2 action has occurred.

### 2. Next steps

1. A distinct reviewer executes TK1-RP16 read-only and returns zero-finding
   ACCEPT or an exact internal repair ledger.
2. On ACCEPT, controller appends review evidence and stages only the three
   approved paths; on REJECT, controller records the finding and opens a
   bounded test-first internal repair packet without asking for duplicate user
   approval.
3. Run staged gates, commit and push exactly TK1, verify upstream equality and
   clean state, then activate TK2 under the approved sequence.

### 3. Approved decisions

- TK-R1/TK-A02 and TK-R2/TK-A03 remain active; TK-R2 is more specific for TK1.
- Final review and deterministic repair gates are internal acceptance points,
  not user approval points.
- P0/P1/P2 must all be zero before Git mutation.

### 4. Execution discipline

- Capability profile remains `native-plan`, `spawn-send-wait`,
  `repo-artifact`, and `native-session-poll` on the Codex adapter.
- Allowlist remains exactly the artifact, `state.py`, and
  `test_task_state.py`; controller alone edits the artifact and test oracle.
- Preserve every finding and stop on unexpected gate red, hash/allowlist drift,
  scope expansion, process ambiguity, or the approved circuit breakers.

## TK1-RP16 Final Review Evidence — TK1-E036

The distinct final read-only reviewer matched artifact
`779f1aa13ba8df4100581a869ed0d8cf0b40b32f8026ac4f211fe8ebea7af597`
at 3720 lines, source
`828dfc407b4568d5300b07ce6b3df7a4024ec9c2959b94c038e8846a25c72ef2`
at 1743 lines, tests
`fbba54ae92a571fdd18906bcd905db10836a2c85f2b90e0e9593fc5b82a4a0e4`
at 3319 lines, branch/HEAD, and exactly the three approved untracked paths. It
returned REJECT with P0=0, P1=1, and P2=1:

1. P1 / TK-D02+TK-D28+TK-D29 / B02+B04+B05+B08: `_enum` accepts
   `isinstance(value, enum_type)`. Python permits an arbitrary object to
   advertise a matching `__class__`, so a proxy can bypass exact enum
   normalization. It can then persist as `reasoning_owner` or `outcome`, leak
   native property failures during serialization, or leak a custom hash failure
   through `next_action_for` and `transition_task`. Exact enum instances have
   `type(value) is enum_type`, so no legitimate member needs the broader test.
2. P2 / TK-D28 / B05: the public `TaskStateError` constructor uses the same
   broad check for `code`. A `__class__` proxy reaches `code.value` and leaks a
   native exception instead of the constructor's declared TypeError.

The reviewer confirmed that the RP15 enum-length and root-unknown-key repairs
are correct and replayed all prior state-machine, provenance, recovery,
immutability, serialization, alias/cycle/depth/node, numeric, translation,
export, and pure-import findings without another issue. Existing Stage 3 raw
ingress/C1 preconstructed-object and later service/CAD residuals are unchanged.
The reviewer used only text/hash/status inspection and performed no edit,
executable validation, Git mutation, CAD/environment/network/model/install,
TK2, or external action.

## Task Packet TK1-RP17 — Exact Enum Identity Closure Under TK-A03

### 1. Authorization

TK-R1/TK-A02 and the more specific TK-R2/TK-A03 authorize closure of the two
TK1-E036 findings inside the existing TK-D02/TK-D28/TK-D29 and B02/B04/B05/B08
scope. This packet inherits all higher-priority system, developer, and user
instructions, applicable directory-scoped instructions, the approved
allowlist, and the current host permission model and sandbox. The Skill,
artifact, and packet cannot grant or expand permissions, elevate authority, or
bypass that model or sandbox. Do not request the same approval again.

### 2. Workspace anchor

- Repository: `/Users/wangtao/Documents/DevProject/vibecad`.
- Branch/HEAD:
  `codex/task-kernel-phase2@bf8967077addfbd8002d6289b0eb925f3f55b638`.
- Starting source:
  `828dfc407b4568d5300b07ce6b3df7a4024ec9c2959b94c038e8846a25c72ef2`,
  1743 lines.
- Starting tests:
  `fbba54ae92a571fdd18906bcd905db10836a2c85f2b90e0e9593fc5b82a4a0e4`,
  3319 lines.
- Allowlist remains exactly the artifact, `src/vibecad/workflow/state.py`, and
  `tests/test_task_state.py`; all other product, dependency, CAD, environment,
  model, network, install, publication, and TK2 scope is prohibited.
- No applicable VibeCAD-scoped `AGENTS.md` or `CLAUDE.md` was observed; current
  host permissions and sandbox remain binding.

### 3. Context

All accepted TK1 enum inputs are either exact members of the requested enum or
exact strings normalized by the requested enum. An object that only advertises
a matching `__class__` is a host object, not durable state. It must be rejected
at the public boundary before hashing, lookup, property access, storage, or
serialization. Public `TaskStateError.code` similarly accepts only an exact
`TaskStateErrorCode` member.

### 4. Steps and gates

1. Controller adds deterministic public-boundary tests first for proxy status,
   event, reasoning owner, criterion outcome, and public error code. Each proxy
   advertises the target enum through `__class__`; hash/value sentinels prove no
   later native operation occurs. Preserve all legitimate string/member tests.
2. A distinct static reviewer confirms current-source reachability, expected
   failures, exact code/path/TypeError assertions, and absence of false-positive
   fixture behavior. P0=P1=P2=0 is required before RED.
3. Run exactly one focused `tests/test_task_state.py` RED. Accept only the five
   newly collected proxy nodes failing for the predicted bypass while all 413
   prior nodes pass. Any collection/setup/syntax mismatch stops the packet.
4. Freeze tests. In `state.py`, replace only the two broad enum/code identity
   checks and any directly corresponding internal enum assertions with exact
   type identity. Do not alter string conversion, error codes/paths, enum
   definitions, mappings, transitions, or public signatures.
5. Use at most two focused GREEN cycles. Then rerun the declared cumulative
   TK1/C1-C5 suite, six readbacks, Ruff check/format, pure/forbidden import,
   broad-exception, diff/hash/allowlist, and a distinct complete final review.
6. ACCEPT requires P0=0/P1=0/P2=0. No stage, Git mutation, push, or TK2 begins
   before final acceptance.

### 5. Execution discipline

- Delegation: `spawn-send-wait`; model tier: `standard` for the bounded repair
  and `deep` for static/final review; process: `native-session-poll` when a live
  command session is returned.
- Test-first, one semantic repair, exact three-file allowlist. Controller alone
  edits the oracle and artifact; production repair occurs only after accepted
  RED and with tests frozen.
- Stop on an unexpected RED, static finding, hash/allowlist drift, scope
  expansion, process ambiguity, second unsuccessful GREEN, or any existing
  circuit breaker.

### 6. Delivery boundary

Complete the deterministic regression, minimum repair, gates, and independent
review only. Controller retains acceptance, staging, commit, push, recovery,
and TK2 authority.

### 7. Final report

Return test/source/artifact hashes and line counts, static prediction, exact
RED/GREEN counts, cumulative/readback/static/import evidence, finding replay,
P0/P1/P2, residuals, prohibited-action confirmation, and workspace state.

## Recovery Snapshot — TK-S017

### 1. Completed milestones

- TK1-RP15 is green and its intended repairs are independently confirmed.
- TK1-RP16 preserved a final gate red with one exact-enum P1 and the same-root
  public-error P2; no Git publication or TK2 action occurred.

### 2. Next steps

1. Controller adds the five TK1-RP17 proxy oracle nodes and freezes a candidate
   hash after a distinct static zero-finding review.
2. Run one predicted RED; on exact match, apply the minimum source-only identity
   repair and execute the full TK1 gates.
3. Obtain final zero-finding ACCEPT, stage only three paths, run staged gates,
   commit/push TK1, verify upstream equality, then activate TK2.

### 3. Approved decisions

- TK-R1/TK-A02 and TK-R2/TK-A03 remain active; this is a bounded internal
  closure under existing fail-closed decisions, not a user approval point.
- Exact enum members and exact strings remain the only enum inputs; public
  `TaskStateError.code` requires an exact `TaskStateErrorCode` member.

### 4. Execution discipline

- Capability profile remains `native-plan`, `spawn-send-wait`,
  `repo-artifact`, and `native-session-poll` on the Codex adapter.
- Preserve the exact three-file allowlist and all RED/review evidence. Stop on
  unexpected gates, drift, expansion, ambiguity, or approved breakers.

## TK1-RP17 Test-Design Candidate — TK1-E037

Controller appended five pytest nodes in two shapes. Tests changed from
`fbba54ae92a571fdd18906bcd905db10836a2c85f2b90e0e9593fc5b82a4a0e4`
at 3319 lines to
`866e2d9bcad6358027fddec87d26e9dc85ff20b6119674e431b00bd4bec8c31a`
at 3379 lines; the first 3319 lines remain byte-exact at the prior hash. Source
remains frozen at
`828dfc407b4568d5300b07ce6b3df7a4024ec9c2959b94c038e8846a25c72ef2`,
1743 lines. The pre-entry artifact was
`09693961ea4599a97ec76ce8110c788b5936653ec75a1dee4baa4129d1200525`
at 3866 lines.

Four parameterized nodes exercise status, event, reasoning-owner, and
criterion-outcome public boundaries with a proxy whose `__class__` advertises
the requested enum and whose hash explodes. Each requires stable INVALID_TYPE
at its fixed public path. One direct public-error node advertises
`TaskStateErrorCode` through `__class__` and explodes on `value`; it requires the
constructor's stable TypeError before property access. Static current-source
prediction is exactly five newly collected failures and all 413 prior nodes
passing. No source, executable, formatter, Git, CAD, environment, network,
model, install, TK2, or external action occurred. A distinct static
P0=0/P1=0/P2=0 ACCEPT is required before the one focused RED.

## TK1-RP17 Static Test Review — TK1-E038

The distinct read-only reviewer returned ACCEPT with P0=0, P1=0, and P2=0.
Artifact
`4de9b1fda9f6b2214b79764c0c79392a9163ab755b0a3f6cd557dc4b3e57f708`
at 3890 lines, frozen source and candidate tests, the byte-exact 3319-line test
prefix, branch/HEAD, and the exact three dirty paths all matched.

It confirmed two shapes and five collected nodes. Python's `isinstance`
consults the advertised `__class__`, making both broad current-source checks
reachable. Status and event reach their hash sentinels; reasoning-owner and
criterion-outcome are stored without raising; public error code reaches its
`value` sentinel instead of TypeError. Correct exact identity rejects all five
before native work while retaining genuine enum members, aliases, and exact
string normalization. There is no fixture/global-state contamination,
collection/name/signature defect, self-trigger, or false-positive oracle.
Static prediction is exactly five new failures and 413 prior passes.

The reviewer performed only text/hash/status inspection and no executable,
edit, Git, CAD, environment, network, model, install, TK2, or external action.
The one focused TK1-RP17 RED is authorized with source frozen.

## TK1-RP17 Focused RED — TK1-E039

Controller preflight matched artifact
`eb48a434066dd3add7fa43ae04adb9ce976d661d6de37de473fe483592f1b554`
at 3912 lines, frozen source
`828dfc407b4568d5300b07ce6b3df7a4024ec9c2959b94c038e8846a25c72ef2`
at 1743 lines, accepted tests
`866e2d9bcad6358027fddec87d26e9dc85ff20b6119674e431b00bd4bec8c31a`
at 3379 lines, branch/HEAD, and the exact three dirty paths.

The single authorized focused command completed normally with exit 1: exactly
5 failed and 413 passed in 2.68 seconds. Status and event reached their hash
sentinels; reasoning-owner and criterion-outcome each failed with DID NOT
RAISE; public error code reached its `value` sentinel. The failures match the
static prediction with no setup, collection, syntax, import, process, or
transport defect. Tests now freeze; only the minimum `state.py` exact-identity
repair and chronological artifact evidence are authorized, with at most two
focused GREEN cycles.

## TK1-RP8 Final Review Rejection — TK1-E013

The distinct final reviewer returned REJECT with P0=0, P1=3, and P2=1 against
artifact 866f6ed4eaaf66198f9e3b3c58e56c13cb59839021e5641318e284461d097a8a,
tests 6fcb989498efb3be05531178c684822a61c0d36878438bcef934d8246de1f620,
and source 8a2d2bf65c015258e455d0e7efb221c5da2b16ff582907c2a3ac179a06e4cd22.
Branch/HEAD and exact three dirty paths matched throughout. The reviewer ran
only text/hash/status inspection and made no mutation or executable call.

Findings:

- P1 / B03+B05 — _guard_exact_nested_containers recursively traverses exact
  raw nested C1/verdict containers before any preflight depth/node budget and
  outside the parser try boundary. A deep builtin tree can leak RecursionError;
  an oversized tree is fully scanned before the actual field parser.
- P1 / B02 — durable program-required invariants omit NEEDS_INPUT. Both
  pre-candidate and candidate-origin histories can be forged with program=None
  even though SUBMIT_PROGRAM is already in their transition provenance.
- P1 / B03+B08 — expected/observed JSON string scalars and object keys, plus
  individual canonical evidence pointers, have no byte limit. Count/depth alone
  therefore do not bound the durable mapping.
- P2 / B05 — _parse_nested catches all Exception subclasses and can disguise a
  trusted parser AssertionError, RuntimeError, or MemoryError as malformed
  input instead of surfacing an implementation failure.

The reviewer confirmed TK1-E002/E004/E010 known failures were otherwise closed
and that TK1-E012's four Ruff-only regions were semantically mechanical. All
four new findings remain inside the explicit bounded/fail-closed/immutable
TK1-B02/B03/B05/B08 outcome; they create no new user-level product, security,
data, cost, dependency, file, environment, external-action, or publication
choice. Review closure continues internally under TK-A03.

## Task Packet TK1-RP9 — Active Final-Review Test-First Closure Under TK-A03

### 1. Authorization

TK-A03 remains the controlling outcome authorization. TK1-RP9 adds only the
missing adversarial oracle and minimum source repair for TK1-E013. It clarifies
already approved TK-D27–TK-D29 bounds; it does not expand Stage C architecture,
public behavior beyond fail-closed validation, dependencies, environment,
files, commit sequence, push, or publication authority.

### 2. Workspace anchor

- Repository: /Users/wangtao/Documents/DevProject/vibecad.
- Branch/HEAD:
  codex/task-kernel-phase2@bf8967077addfbd8002d6289b0eb925f3f55b638.
- Starting tests:
  6fcb989498efb3be05531178c684822a61c0d36878438bcef934d8246de1f620.
- Frozen source:
  8a2d2bf65c015258e455d0e7efb221c5da2b16ff582907c2a3ac179a06e4cd22.
- Starting artifact:
  866f6ed4eaaf66198f9e3b3c58e56c13cb59839021e5641318e284461d097a8a.
- Test-design phase permits controller edits only to test_task_state.py and
  this artifact. Source and executable validation remain frozen until static
  test-design ACCEPT.

### 3. Context

The contract already promises strict, bounded, JSON-compatible, immutable
verdict data and no native exception leakage for malformed admissible input.
The concrete internal limits selected for this closure are:

- nested exact-container preflight: 72 container levels and 4096 total nodes,
  leaving structural headroom beyond the existing 64-level field JSON budget;
- expected/observed JSON string scalar: 4096 UTF-8 bytes;
- expected/observed JSON object key: 256 UTF-8 bytes;
- each evidence JSON Pointer: 256 UTF-8 bytes.

These finite limits keep the maximum durable footprint bounded while preserving
ordinary CAD verifier facts. Exact maxima must pass; max+1 ASCII and multibyte
forms must fail with stable TaskStateError. Invalid Unicode must not leak an
encoding exception.

### 4. Steps and gates

1. Controller adds tests only for:
   - nested VerificationReport/verdict raw depth and node preflight using a late
     hostile sentinel that must not be reached after the budget is exceeded;
   - pre-candidate and candidate-origin NEEDS_INPUT mappings with program=None;
   - exact/max+1 ASCII and multibyte JSON strings, object keys, and evidence
     pointers, including invalid Unicode containment;
   - direct _parse_nested trusted parser AssertionError propagation proving no
     broad exception translation.
2. Do not run pytest/Python/Ruff/formatter yet. A distinct read-only reviewer
   must return P0=0/P1=0/P2=0 for test correctness, budget precedence, fixture
   independence, expected code/path, and full TK1-E013 coverage.
3. After static ACCEPT, run exactly one focused repair RED. It must collect
   normally and fail only on TK1-E013. The expected native signature is the
   deliberately injected trusted-parser AssertionError, which the current broad
   catch wrongly converts; no uncontrolled RecursionError/Unicode error is an
   acceptable harness outcome.
4. Edit only state.py. Replace recursive raw preflight with a bounded iterative
   exact-container walk; require program for every NEEDS_INPUT history; enforce
   the selected UTF-8 byte limits; and remove broad Exception translation while
   preserving ContractValidationError and TaskStateError prefix handling.
5. At most two focused GREEN cycles follow RED. A failed second cycle freezes
   TK1. On focused GREEN, run cumulative TK1/C1–C5, six readback tests, two-file
   Ruff check/format, pure/forbidden import, diff/allowlist, and hash gates.
6. Assign a distinct deep read-only final reviewer to replay TK1-E013 plus all
   prior final-review surfaces. ACCEPT requires P0=0/P1=0/P2=0.
7. Only after ACCEPT may controller append final evidence, stage exact three
   files, rerun staged gates, create the original TK1 commit, push non-force,
   verify upstream equality, and activate TK2.

### 5. Execution discipline

- Test-first order is mandatory; static test acceptance precedes RED.
- Source cannot change during test design; tests cannot change after RED.
- Stop on static rejection after two repair cycles, unexpected RED, failed
  second GREEN, native leakage other than the deliberate trusted-parser test,
  hash drift, out-of-allowlist write, material boundary, or process ambiguity.
- Never reset/discard/amend/force-push, hide evidence, install/change
  environment, execute CAD/network/model actions, stage, commit, push, or start
  TK2 before final ACCEPT.

### 6. Delivery boundary

Deliver only the TK1-E013 adversarial tests, pure state.py repair, repeated
local gates, and final review. No service, persistence, MCP, CAD execution, or
user-facing feature is added.

### 7. Final report

Return before/after hashes; test categories and static verdict; exact RED and
each GREEN count/signature; source changes; cumulative/readback/static/import
gates; finding replay; residuals; prohibited-action confirmation; and final
branch/HEAD/status.

## Recovery Snapshot — TK-S013

### 1. Completed milestones

- TK1-RP8 independently rejected three P1 and one P2 before Git mutation.
- All previous functional/static gates and exact evidence remain preserved.
- No source repair, new RED, Git publication, or TK2 action has occurred yet.

### 2. Next steps

1. Add and statically accept the four-category TK1-E013 test oracle.
2. Run one repair RED and close source within two GREEN cycles.
3. Repeat all gates and obtain a new deep zero-finding final ACCEPT.

### 3. Approved decisions

- TK-R1/TK-A02 and TK-R2/TK-A03 remain active.
- The selected finite byte/preflight budgets clarify D27–D29 internally.
- No additional user approval is required for this fail-closed closure.

### 4. Execution discipline

- Source remains frozen until static test-design ACCEPT.
- Tests freeze immediately after ACCEPT and RED.
- No external/environment/CAD/model/Git action before final ACCEPT.

## TK1-RP9 Test-Design Candidate — TK1-E014

Controller changed only tests/test_task_state.py from
6fcb989498efb3be05531178c684822a61c0d36878438bcef934d8246de1f620 to
3853f421c0e06ba9f03ae9c9a7d44c70143e8cb9441fa14ebc37f05a3de2060c,
2923 lines. Source remained frozen at
8a2d2bf65c015258e455d0e7efb221c5da2b16ff582907c2a3ac179a06e4cd22.

The candidate oracle adds:

- exact 64-level nested verdict JSON acceptance, preflight depth 73 with a late
  hostile sentinel, a caught 2048-level RecursionError-leak assertion, and a
  4096-node late-sentinel preflight case;
- both pre-candidate and candidate-origin NEEDS_INPUT program=None forgeries;
- exact and max+1 ASCII/multibyte UTF-8 budgets for JSON scalar strings, object
  keys, and individual evidence pointers, plus invalid-surrogate containment;
- a deliberate trusted parser AssertionError that _parse_nested must propagate
  rather than translate.

No pytest, Python, Ruff, formatter, source edit, CAD, network, model, install,
stage, commit, push, or TK2 action occurred. A distinct static reviewer must
return P0=0/P1=0/P2=0 before RED.

## TK1-RP9 Static Test-Design Review 1 — TK1-E015

The distinct reviewer returned REJECT with P0=0, P1=3, and P2=0 against
candidate test hash
3853f421c0e06ba9f03ae9c9a7d44c70143e8cb9441fa14ebc37f05a3de2060c.
Source remained frozen. The reviewer confirmed all individual fixture values,
paths, UTF-8 calculations, 64-level acceptance, late sentinels, caught native
leak, and trusted-parser test were locally sound, but found RED observability
gaps:

- depth-73 failure would stop the aggregate test before the 2048-depth and
  4096-node cases;
- pre-candidate NEEDS_INPUT failure would stop before candidate-origin;
- first ASCII max+1 failure would stop before multibyte/key/surrogate cases,
  and invalid-surrogate object-key coverage was absent.

No executable or mutation occurred. Controller static repair cycle 2 split
every shape into an independent test or parametrized pytest node and added the
invalid-surrogate object-key branch. The candidate test hash is now
5be8d25741aa19a14b1ec832b3f66308965b53aa2aae0ad50eab7fec4dc5b973,
2961 lines. Source remains
8a2d2bf65c015258e455d0e7efb221c5da2b16ff582907c2a3ac179a06e4cd22.
No pytest, Python, Ruff, formatter, source edit, Git mutation, or external action
occurred. Static cycle 2 must return zero findings before RED.

## TK1-RP9 Static Test-Design Acceptance — TK1-E016

Static review cycle 2 returned ACCEPT with P0=0, P1=0, and P2=0 against tests
5be8d25741aa19a14b1ec832b3f66308965b53aa2aae0ad50eab7fec4dc5b973,
2961 lines. Artifact was
09d7d7e8ec5cc1931c5f9b3d8b9eafb4169ae83d404e6bf919ca28f79750b726
and source remained frozen at
8a2d2bf65c015258e455d0e7efb221c5da2b16ff582907c2a3ac179a06e4cd22.

The reviewer confirmed four independent preflight nodes, two independent
NEEDS_INPUT provenance nodes, individually collected exact/max+1/surrogate
string/key/pointer nodes, and the independent trusted-parser propagation node.
The original formatted oracle prefix remained byte-identical. Current-source
failures are controlled pytest/assertion outcomes; the extreme-depth test
catches RecursionError rather than leaking it from the harness. No executable,
edit, Git, CAD, network, model, install, or external action occurred. The one
TK1-RP9 repair RED is now authorized.

## TK1-RP9 Repair RED Evidence — TK1-E017

Controller launched the one authorized command once:

    PYTHONPATH=src .venv/bin/pytest -q tests/test_task_state.py

The process completed and no pytest process remained, but the command transport
returned only progress through 93 percent plus the first failure and lost the
normal footer/session handle. The controller did not relaunch or duplicate the
RED. Read-only recovery from pytest's existing lastfailed cache identified all
15 newly accepted failing nodes and no unhandled setup/collection/import node:

- depth-73, caught depth-2048 RecursionError leak, and 4096-node preflight;
- both NEEDS_INPUT program provenance nodes;
- four scalar/key max+1 byte nodes and two invalid-Unicode nodes;
- two evidence-pointer max+1 nodes and its invalid-Unicode node;
- trusted parser AssertionError propagation.

The pre-existing oracle prefix has 362 nodes and TK1-E016 adds 22 independently
collected nodes, so this run's recovered result is 369 passed and 15 failed out
of 384. Exact-depth, exact string/key/pointer budgets, and every prior oracle
node passed. Two unrelated/stale lastfailed keys were explicitly excluded: one
belongs to test_tool_result_normalizer.py outside the focused command and one
is an obsolete task-state parametrization key. Source/tests/artifact hashes
remained unchanged after the run. The recovered RED matches only TK1-E013, so
minimum state.py repair is authorized without repeating the command.

## TK1-RP9 Implementation and Gate Evidence — TK1-E018

The implementation worker matched all anchors, did not repeat RED, froze tests
and artifact, and edited only state.py with apply_patch. GREEN cycle 1 passed
384 tests at source hash
8a3c9cab70626bd60c8e606cb677c35ec509a8594150b4c45450a0e721f83279.
Ruff then identified one source-only 101-character line; after a mechanical
apply_patch line wrap, the final allowed GREEN cycle 2 passed all 384 tests.
Final source hash is
7fd9e1aafe7652830eac546e1b18613cbb25579f65b0e1ea50e1f9b5527bac00,
1703 lines.

The repair:

- replaces recursive raw nested preflight with an exact-type iterative walk
  bounded at 72 container levels and 4096 total nodes;
- requires program in both NEEDS_INPUT provenance families;
- bounds verdict JSON strings at 4096 UTF-8 bytes, object keys at 256, and each
  evidence pointer at 256, with controlled invalid-Unicode rejection;
- removes broad Exception/BaseException translation while preserving defined
  ContractValidationError and TaskStateError prefix translation.

Post-GREEN gates:

- focused final: 384 passed;
- cumulative TK1/C1–C5: 773 passed, 1 deselected;
- six mutation/round-trip readbacks: 6 passed;
- two-file Ruff check and format check: passed/already formatted;
- fresh pure import: passed; forbidden imported-module list: empty;
- git diff --check: passed; broad exception search: empty;
- tests remained
  5be8d25741aa19a14b1ec832b3f66308965b53aa2aae0ad50eab7fec4dc5b973;
- artifact remained
  9469aafb22a842bce8bb98474a6bbee2a7f591ad61ea995f0e7913921c9672aa;
- branch/HEAD and exact three dirty paths remained unchanged.

No test/artifact edit, RED rerun, dependency/environment change, CAD/network/
model action, stage, commit, push, or TK2 action occurred.

## Task Packet TK1-RP10 — Active Final Zero-Finding Acceptance Review

### 1. Authorization

TK-A03 authorizes a final read-only review of the complete TK1 outcome after
TK1-E018. The reviewer may inspect text, hashes, status, and history evidence
only. No execution, mutation, Git write, environment, CAD, network, model, or
publication action is authorized.

### 2. Workspace anchor

- Repository: /Users/wangtao/Documents/DevProject/vibecad.
- Branch/HEAD:
  codex/task-kernel-phase2@bf8967077addfbd8002d6289b0eb925f3f55b638.
- Review tests:
  5be8d25741aa19a14b1ec832b3f66308965b53aa2aae0ad50eab7fec4dc5b973,
  2961 lines.
- Review source:
  7fd9e1aafe7652830eac546e1b18613cbb25579f65b0e1ea50e1f9b5527bac00,
  1703 lines.
- Reviewer must report current artifact hash/line count and exact dirty paths.

### 3. Context

The reviewer must replay every TK1-RP8 finding, TK1-E013 test gap, TK1-E016
oracle, TK1-E017 recovered RED, and TK1-E018 repair, then regress all earlier
TK1-E002/E004/E009/E010 findings. Passing tests cannot by itself disposition a
finding. Special attention is required for iterative budget off-by-one,
fail-before-iteration behavior, invalid Unicode, error prefixing, and durable
NEEDS_INPUT provenance.

### 4. Steps and gates

1. Reconfirm branch/HEAD, hashes, line counts, exact three dirty paths, and no
   unrelated drift through read-only inspection.
2. Review the iterative nested walk for exact builtin checks before iteration,
   finite depth/node semantics, cycle/alias behavior, deterministic root paths,
   and preservation of valid 64-level field JSON.
3. Review UTF-8 byte validation for ASCII/multibyte exact/max+1, invalid
   surrogates, scalar versus key branches, evidence canonicality, and absence of
   native encoding leakage or unbounded durable content.
4. Review both NEEDS_INPUT histories and all candidate/report/AcceptanceSpec/
   artifact/step/transition/last_error/reconciliation invariants.
5. Confirm broad trusted-parser bugs are no longer hidden while declared nested
   contract errors retain stable fully prefixed TaskStateError paths.
6. Review test independence and prior finding replay. Return ACCEPT only with
   P0=0/P1=0/P2=0; otherwise give exact severity/lines/failure shape/B mapping.
7. Confirm no edit, pytest/Python/Ruff/formatter execution, Git mutation,
   install, CAD/network/model action, or external side effect occurred.

### 5. Execution discipline

- Distinct deep reviewer, text/hash/status inspection only.
- Do not delegate to the implementation worker or cite green tests as the sole
  basis for closure.
- Any P0/P1/P2, hash drift, hidden broad catch, out-of-allowlist path, semantic
  oracle defect, or material boundary means REJECT.
- Never edit, stage, commit, push, reset, amend, install, or start TK2.

### 6. Delivery boundary

Deliver only exact findings and ACCEPT/REJECT. Controller retains artifact,
repair, staged-gate, commit, push, and TK2 authority.

### 7. Final report

Return anchors; P0/P1/P2; line-level E013 and historical replay; new bypass
analysis; test adequacy; residual risks; prohibited-action confirmation; and
branch/HEAD/status.

## Recovery Snapshot — TK-S014

### 1. Completed milestones

- TK1-E013 oracle reached static ACCEPT and genuine RED.
- Source repair reached 384/0 and cumulative/static/import/readback GREEN.
- No Git mutation or TK2 action has occurred.

### 2. Next steps

1. Distinct reviewer returns zero-finding ACCEPT or exact repair evidence.
2. On ACCEPT, controller appends final ledger and runs staged gates.
3. Commit/push TK1 exactly, verify upstream, then activate TK2.

### 3. Approved decisions

- TK-R1/TK-A02 and TK-R2/TK-A03 remain active.
- TK1-RP10 is internal acceptance, not a user approval point.
- P0/P1/P2 must all be zero before Git mutation.

### 4. Execution discipline

- Source/tests remain frozen during review.
- Preserve every finding and failed gate.
- No external/environment/CAD/model/Git action before ACCEPT.

## TK1-RP10 Final Review Rejection — TK1-E019

The distinct reviewer returned REJECT with P0=0, P1=2, and P2=0 against
artifact 958d8a7fe3ff83f7a5788b4a938b49cc41a93e4ccbb0a35de169e082cc369306,
tests 5be8d25741aa19a14b1ec832b3f66308965b53aa2aae0ad50eab7fec4dc5b973,
and source 7fd9e1aafe7652830eac546e1b18613cbb25579f65b0e1ea50e1f9b5527bac00.
All anchors and exact dirty paths matched; no executable or mutation occurred.

- P1 / B03+B05 — nested preflight skips descendants of a repeated container
  identity. The downstream C1 JSON freezer allows non-cyclic aliases and can
  recursively expand a depth-60 binary alias DAG despite the apparent 4096-node
  preflight bound. Cycles are contained, but alias CPU/memory amplification is
  not. Reject repeated raw container identity before the parser.
- P1 / B03+B05+B08 — UTF-8 validation fully encodes a string before comparing
  its byte limit, and nested preflight constructs/escapes a complete mapping-key
  path before enforcing any key byte limit. Oversized scalar/key/pointer input
  can therefore allocate proportional duplicate data or leak resource failure
  before deterministic rejection.

Every earlier E013 and historical invariant was otherwise confirmed closed.
Both findings are the same approved bounded/fail-before-work outcome and require
no new user-level decision.

## Task Packet TK1-RP11 — Active Alias and Fail-Early Closure Under TK-A03

### 1. Authorization

TK-A03 remains controlling. TK1-RP11 adds only adversarial tests and minimum
state.py closure for TK1-E019. It changes no public feature, dependency,
environment, file allowlist, commit sequence, external action, or publication
boundary.

### 2. Workspace anchor

- Repository: /Users/wangtao/Documents/DevProject/vibecad.
- Branch/HEAD:
  codex/task-kernel-phase2@bf8967077addfbd8002d6289b0eb925f3f55b638.
- Starting artifact:
  958d8a7fe3ff83f7a5788b4a938b49cc41a93e4ccbb0a35de169e082cc369306.
- Starting tests:
  5be8d25741aa19a14b1ec832b3f66308965b53aa2aae0ad50eab7fec4dc5b973.
- Frozen source:
  7fd9e1aafe7652830eac546e1b18613cbb25579f65b0e1ea50e1f9b5527bac00.
- Controller may edit tests and artifact in test-design phase; source and all
  executable validation remain frozen until static ACCEPT.

### 3. Context

A Python object graph is not necessarily a JSON tree. Since downstream C1
accepts sibling aliases, nested preflight must reject every repeated container
identity, not merely stop traversing it. UTF-8 bytes are never fewer than Python
code points, so len(value)>maximum is a safe allocation-free early rejection;
only strings with at most maximum code points may be encoded for exact byte
measurement. Raw nested mapping keys must pass that check before pointer escape
or construction, and overlong error tokens must truncate before joining.

### 4. Steps and gates

1. Add independent tests only for:
   - a reachable raw /steps/0/result/value binary alias DAG rejected at the
     second reference before C1 expansion;
   - an over-limit str subclass whose encode method explodes, proving
     _validate_utf8_budget rejects by len before encode at both 256 and 4096;
   - a nested over-limit exact string key with _error_pointer replaced by an
     exploding sentinel, proving key budget precedes pointer construction;
   - an overlong pointer token whose replace method explodes, proving
     _error_pointer truncates before token escaping while preserving the prior
     UNKNOWN_FIELD bounded-output behavior for ordinary exact strings.
2. Do not run pytest/Python/Ruff/formatter. A distinct static reviewer must
   return P0=0/P1=0/P2=0 for independent collection, fixture safety, stable
   code/path, and complete E019 coverage before RED.
3. Run one focused RED. It must collect normally and fail only on E019; injected
   sentinel exceptions must be contained as ordinary pytest failures, not crash
   collection or leave a live process.
4. Edit only state.py: reject repeated raw container identities; add code-point
   short-circuit before UTF-8 encoding; validate exact raw keys before pointer
   construction; and bound overlong tokens before join/escape.
5. At most two focused GREEN cycles follow RED. On focused GREEN run cumulative
   TK1/C1–C5, six readbacks, two-file Ruff, pure/forbidden import, diff/hash/
   allowlist, and broad-catch gates.
6. Obtain another distinct deep final review over E019 and all prior findings.
   ACCEPT requires P0=0/P1=0/P2=0.
7. Only after ACCEPT may controller append acceptance evidence, stage the exact
   three files, rerun staged gates, commit/push TK1, verify upstream, and start
   TK2.

### 5. Execution discipline

- Static test acceptance before RED; tests freeze after acceptance.
- Source-only implementation after RED; two GREEN cycles maximum.
- Stop on static rejection after two cycles, unexpected RED, failed second
  GREEN, uncontrolled sentinel/native exception, hash drift, material boundary,
  out-of-allowlist write, or process ambiguity.
- Never reset/discard/amend/force-push, hide evidence, install/change
  environment, execute CAD/network/model actions, stage, commit, push, or start
  TK2 before final ACCEPT.

### 6. Delivery boundary

Deliver only E019 tests, pure state.py repair, local gates, and review. No user-
facing feature, service, persistence, MCP, or CAD execution is introduced.

### 7. Final report

Return hashes; static verdict; RED/GREEN commands/counts; alias and allocation-
order source proof; cumulative/readback/static/import gates; final review;
residuals; prohibited-action confirmation; and branch/HEAD/status.

## Recovery Snapshot — TK-S015

### 1. Completed milestones

- TK1-RP10 confirmed all but two bounded-resource findings.
- Prior GREEN and review evidence remains preserved without Git mutation.
- TK1-RP11 is active test-first closure.

### 2. Next steps

1. Add/statically accept four independent E019 test shapes.
2. Run one RED, repair state.py within two GREEN cycles, repeat gates.
3. Obtain zero-finding final review, then commit/push TK1.

### 3. Approved decisions

- TK-R1/TK-A02 and TK-R2/TK-A03 remain active.
- Repeated raw aliases are invalid; fail-early order is required.
- No user approval is needed for this internal bounded-input closure.

### 4. Execution discipline

- Source frozen until static test ACCEPT; tests freeze thereafter.
- No external/environment/CAD/model/Git action before final ACCEPT.

## TK1-RP11 Test-Design Candidate — TK1-E020

Controller appended only six independently collected pytest nodes across four
test shapes. Tests changed from
5be8d25741aa19a14b1ec832b3f66308965b53aa2aae0ad50eab7fec4dc5b973 to
1ee3947d7e2108e7cf431b92d7583e81bda2035318d9833ad36ff2be16a7c39a,
3029 lines. Source remained frozen at
7fd9e1aafe7652830eac546e1b18613cbb25579f65b0e1ea50e1f9b5527bac00.

The candidate covers reachable /steps/0/result/value repeated alias rejection;
256/4096 code-point short-circuit before an exploding encode; ASCII and
multibyte nested-key rejection before an exploding pointer builder; and
overlong error-token truncation before an exploding replace. No executable,
source edit, formatter, Git, CAD, network, model, install, or external action
occurred. Static zero-finding ACCEPT is required before RED.

## TK1-RP11 Static Test Review Cycle 1 and Candidate 2 — TK1-E021

The distinct read-only reviewer returned REJECT with P0=0, P1=1, and P2=1.
The P1 finding was that the alias-DAG test proved the final controlled error but
did not prove that duplicate-identity rejection preceded the nested parser; a
defective parse-then-reject repair could satisfy the original assertion. The P2
finding was an evidence count typo: TK1-E020 said seven new pytest nodes, while
the four test shapes collect as 1 + 2 + 2 + 1 = six. All other candidate tests,
anchors, frozen-source checks, and prohibited-action checks were accepted.

Before any executable action, the controller corrected the TK1-E020 count from
seven to six and added an exploding `TaskStepRecord.from_mapping` sentinel to
the alias-DAG test. A conforming outer guard must now reject the repeated raw
container identity at `/steps/0/result/value/1` before the sentinel can run.
Tests changed from
1ee3947d7e2108e7cf431b92d7583e81bda2035318d9833ad36ff2be16a7c39a to
2296febdf9508b965f0a18dc4068397d46256784af169949fc32c60ec06905da,
3033 lines. Source remained frozen at
7fd9e1aafe7652830eac546e1b18613cbb25579f65b0e1ea50e1f9b5527bac00.
No test, Python, Ruff, formatter, source edit, Git mutation, CAD, network,
model, install, or external action occurred. A second static zero-finding
ACCEPT is required before the single focused RED.

## TK1-RP11 Static Test Review Cycle 2 — TK1-E022

The same distinct read-only reviewer returned ACCEPT with P0=0, P1=0, and
P2=0. Branch, HEAD, the exact three dirty paths, and all frozen anchors matched:
artifact
c2d99f2c8eecf6e9b426600662c63f0833675d01d0e82bb55cab8e4f9320e5d5 at
3106 lines; tests
2296febdf9508b965f0a18dc4068397d46256784af169949fc32c60ec06905da at
3033 lines; and source
7fd9e1aafe7652830eac546e1b18613cbb25579f65b0e1ea50e1f9b5527bac00 at
1703 lines.

The reviewer confirmed that the parser sentinel closes the Cycle 1 ordering
finding, the evidence transparently corrects the collected-node count to six,
and all six nodes have deterministic current-source RED and repair GREEN
behavior with the required codes and paths. Fixture isolation, syntax, static
format, and prohibited-action boundaries were accepted. The reviewer performed
no executable, mutation, Git, CAD, network, model, install, or external action.
The single focused TK1-RP11 RED is now authorized with source frozen.

## TK1-RP11 Focused RED — TK1-E023

Controller preflight matched branch
`codex/task-kernel-phase2`, HEAD
`bf8967077addfbd8002d6289b0eb925f3f55b638`, the exact three dirty paths,
artifact
`c369ff1badfa0738e09aef61be7379e755687590a3929042423d408e7d9aa165` at
3126 lines, tests
`2296febdf9508b965f0a18dc4068397d46256784af169949fc32c60ec06905da` at
3033 lines, and frozen source
`7fd9e1aafe7652830eac546e1b18613cbb25579f65b0e1ea50e1f9b5527bac00`
at 1703 lines.

The one authorized command was
`PYTHONPATH=src .venv/bin/pytest -q tests/test_task_state.py`. It completed
normally with exit 1: 384 passed and exactly six failed in 2.11 seconds. The
failures were the one parser-order alias sentinel, both len-before-encode UTF-8
budgets, both key-budget-before-pointer cases, and the one
truncate-before-escape token case. Each failed at its intended exploding
sentinel in the frozen source; no pre-existing test failed, and there was no
collection, process, or transport failure. This is the genuine TK1-RP11 RED.

Tests now freeze at the accepted hash. The repair boundary is source-only apart
from chronological evidence updates, with at most two focused GREEN cycles.
No formatter, Git mutation, CAD, network, model, install, or external action
occurred.

## TK1-RP11 Implementation and Gate Evidence — TK1-E024

The controller changed only `src/vibecad/workflow/state.py` after the genuine
RED, while the accepted tests remained byte-frozen. The minimum repair rejects
every repeated exact raw container identity at its second path before nested
parsing; rejects strings whose code-point count already exceeds a UTF-8 byte
budget before encoding; validates exact string keys before constructing their
error pointers; and substitutes the stable truncated pointer token before work
on an overlong token.

Focused GREEN cycle 1 passed all 390 tests in 1.41 seconds at source
`d25db0ba9daa89ba5d33a91344ca08a957f03c57a4d91eb86187b228223995da`,
1719 lines. No second GREEN cycle was needed. Post-GREEN gates then passed:

- cumulative TK1/C1-C5: 779 passed, 1 deselected in 2.27 seconds;
- the six preserved mutation/round-trip readbacks: 6 passed in 0.10 seconds;
- two-file Ruff check: all checks passed;
- two-file Ruff format check: both files already formatted;
- fresh pure import: `task state pure import OK`;
- forbidden imported-module list: empty;
- broad Exception/BaseException search: empty;
- `git diff --check`: exit 0;
- tests remained
  `2296febdf9508b965f0a18dc4068397d46256784af169949fc32c60ec06905da`,
  3033 lines;
- branch/HEAD remained
  `codex/task-kernel-phase2@bf8967077addfbd8002d6289b0eb925f3f55b638`;
- dirty paths remained exactly the artifact, source, and test file.

The artifact immediately before this evidence update was
`6b6dcc464b485d0af8137a13059f3c4c6817a7e08926c4084eb5e0cd3d07d128`,
3153 lines. No test edit, dependency/environment change, CAD/network/model
action, stage, commit, push, or TK2 action occurred. Source and tests now freeze
for a distinct zero-finding final review.

## Task Packet TK1-RP12 — Final Zero-Finding Acceptance Review Under TK-A03

### 1. Authorization and boundary

TK-A03 authorizes a distinct final read-only review of the complete TK1 outcome
after TK1-E024. The reviewer may inspect repository text, hashes, status, and
recorded evidence. The reviewer may not edit, run pytest/Python/Ruff/formatter,
mutate Git, touch CAD or the environment, use network/model/install actions, or
start TK2.

### 2. Required review

1. Reconfirm branch/HEAD, exact three dirty paths, and the supplied source,
   tests, and artifact anchors.
2. Re-review all of `state.py` against TK-D01-TK-D32 and TK1-B01-TK1-B10,
   including every finding and repair through TK1-RP11. Passing evidence alone
   cannot disposition a finding.
3. Trace raw nested preflight ordering, exact-container enforcement,
   node/depth/alias handling, key/string budgets, bounded error rendering, and
   nested parser exception translation. Explicitly attempt to bypass each of
   the four RP11 repairs without executing code.
4. Recheck immutable durable state, candidate/program/report/artifact/error
   provenance, the full transition/next-action matrix, recovery semantics,
   public exports, pure-import boundaries, and serialization round trips.
5. Check the accepted tests for false positives, missing reachable variants,
   fixture contamination, or assertions that an incorrect implementation could
   satisfy. Confirm that tests remain byte-frozen after RED.
6. Return ACCEPT only with P0=0, P1=0, and P2=0. Otherwise return REJECT with
   exact source/test lines, a concrete failure shape, and ledger mapping.

### 3. Delivery

Return exact hashes/line counts/status, prior-finding replay, new adversarial
analysis, residual risks, and prohibited-action confirmation. The controller
alone owns evidence updates, staged gates, commit, and push.

## TK1-RP12 Final Review Evidence — TK1-E025

The distinct read-only reviewer matched artifact
`0a1f0d0b2bbdb5e643dd380c3728eb4521becf5f706cc8d1c5012c0855b23c33`
at 3224 lines, source
`d25db0ba9daa89ba5d33a91344ca08a957f03c57a4d91eb86187b228223995da`
at 1719 lines, tests
`2296febdf9508b965f0a18dc4068397d46256784af169949fc32c60ec06905da`
at 3033 lines, branch/HEAD, and the exact three dirty paths. Final review
returned REJECT. Its corrected ledger is P0=0, P1=4, and P2=1:

1. P1 / TK-D27 / B03+B05: `_bounded_sequence` copies an exact list to a
   tuple before checking its hard length budget.
2. P1 / TK-D28+TK-D29 / B03+B05+B08: `_text` strips before its length check,
   and raw nested scalar strings can reach a C1 parser and proportional error
   construction without an outer bound.
3. P1 / TK-D29 / B03+B05+B08: three state integer checks use `abs()` before
   safe-range rejection, while nested C1 integers lack an outer direct-range
   preflight.
4. P1 / TK-D28 / B03+B05: strict root mappings snapshot, set-convert, and sort
   an unbounded field population before rejecting it.
5. P2 / TK-D28 / B05: the public `TaskStateError` constructor performs
   canonical scanning and message stripping before rejecting overlong direct
   path/message inputs. This fifth finding was initially dispositioned as a
   trusted-caller case, then correctly restored because `TaskStateError` is a
   public export; the correction is preserved here rather than hidden.

The reviewer confirmed that RP10/RP11 repairs, every state/provenance/
transition/recovery invariant, exact-container/depth/node behavior, immutable
round trips, and public exports otherwise hold. A supplemental suggestion to
make missing candidate-bound identifiers generic MISSING_FIELD was rejected as
contrary to explicit TK-D25 and the accepted missing/null/type/malformed oracle.
No reviewer executed tests or modified files, Git, CAD, environment, network,
model, install, or external state.

## Task Packet TK1-RP13 — Proportional-Work Closure Under TK-A03

### 1. Authorization and outcome

TK-A03 remains controlling. TK1-RP13 closes only the five TK1-E025 findings
inside TK-D27-TK-D29 and B03/B05/B08. It changes no architecture, public task
state, dependency, environment, CAD/model/network behavior, commit count, or
external action.

### 2. Selected internal behavior

- Exact list/tuple type and `len` budget precede defensive tuple copying.
- Root schema mappings have a private 64-field preflight ceiling. Inputs above
  it fail with BUDGET_EXCEEDED at the mapping root before key snapshots, sets,
  sorting, or pointer construction; normal strict-field behavior below it is
  unchanged.
- `_text` and public error messages check the existing 256-character limit
  before strip/printability/line work. A directly constructed public error path
  above the existing 256-character ceiling is rejected before canonical scan.
- Raw nested scalar strings use the existing 4096-byte rich-JSON ceiling before
  C1 parsing. Unsafe exact integers use direct lower/upper comparison at their
  current path before C1 parsing. No edit to `contracts.py` is authorized.
- Existing error codes, valid boundaries, JSON-pointer composition, node/depth
  accounting, aliases, and exact-type policy remain unchanged.

### 3. Test-first and execution gates

1. Controller adds only deterministic order tests using exploding sentinels;
   no test may depend on an actual memory exhaustion or long runtime.
2. Source remains frozen until a distinct static reviewer returns
   P0=0/P1=0/P2=0. At most two static design cycles are available.
3. Run exactly one focused RED. It must fail only at the newly accepted
   sentinels, with every prior node passing and normal collection/process exit.
4. Freeze tests and apply the minimum `state.py`-only repair. At most two
   focused GREEN cycles are available.
5. On GREEN, rerun cumulative TK1/C1-C5, six preserved readbacks, two-file Ruff
   check/format, pure/forbidden import, broad-exception, diff, hash, and exact
   allowlist gates.
6. A distinct deep final review must return P0=0/P1=0/P2=0 before staging.

### 4. Prohibited actions and delivery

No stage, commit, push, TK2, dependency/environment change, CAD, network,
model, install, or external action is allowed before final ACCEPT. Preserve
every RED/GREEN/review result in this artifact. Controller alone may edit the
artifact and accepted tests; production repair is source-only after RED.

## TK1-RP13 Test-Design Candidate — TK1-E026

Controller appended 12 independently collected pytest nodes across ten test
shapes. Tests changed from
`2296febdf9508b965f0a18dc4068397d46256784af169949fc32c60ec06905da` to
`21779ba976b7384302a5f3bc4ece52e8dafe621a743189fb0c6dc4265a062cb6`,
3204 lines. Source remained frozen at
`d25db0ba9daa89ba5d33a91344ca08a957f03c57a4d91eb86187b228223995da`,
1719 lines. The artifact before this evidence update was
`e5d970fef4fbd807deacc089245b432b312b0a4501f361caa1b1ce1c4a57925c`,
3306 lines.

The failing order oracles cover length-before-tuple, length-before-strip,
public path-length-before-canonical-scan, public message-length-before-strip,
nested string/int rejection before a C1 parser sentinel, three direct
negative-int-before-abs sites, and root mapping population before key snapshot.
Two non-RED boundary shapes preserve the exact 64-field private mapping limit
and the existing public error/nested string/int exact maxima. All sentinels use
bounded ordinary inputs; none depends on memory exhaustion or timing. No test,
Python, Ruff, formatter, source edit, Git mutation, CAD, network, model,
install, or external action occurred. Static P0=0/P1=0/P2=0 ACCEPT is required
before RED.

## TK1-RP13 Static Review Cycle 1 and Candidate 2 — TK1-E027

The distinct static reviewer matched every anchor and returned REJECT with
P0=0, P1=3, and P2=0. It required text sentinels to cover `strip`,
`isprintable`, and `splitlines` plus the exact `_text` 256 boundary; reachable
nested scalar tests to cover both ASCII and multibyte UTF-8 max+1/exact bytes;
and the nested integer test to explode on `abs` plus direct exact negative
safe-integer boundaries at all three repaired state sites. The reviewer accepted
the other RED behavior, paths, codes, reachable raw C1 shape, fixture isolation,
and original count of 12 nodes.

Before any executable action, controller closed all three findings. It also
strengthened the public path sentinel against premature `_bounded_error_path`
work and the mapping sentinel against tuple, set, sort, or pointer work, so a
reordered but still proportional repair cannot false-green. Candidate 2 has 17
independently collected nodes across 12 test shapes. Tests changed from
`21779ba976b7384302a5f3bc4ece52e8dafe621a743189fb0c6dc4265a062cb6` to
`0549002b19975ac3e46a04c80221cf975ca7f5009b6cfa4493aee236a5d869de`,
3256 lines. Source remained frozen at
`d25db0ba9daa89ba5d33a91344ca08a957f03c57a4d91eb86187b228223995da`,
1719 lines. No test, Python, Ruff, formatter, source edit, Git mutation, CAD,
network, model, install, or external action occurred. Cycle 2 static
P0=0/P1=0/P2=0 ACCEPT is required before RED.

## TK1-RP13 Static Review Cycle 2 — TK1-E028

The distinct read-only reviewer returned ACCEPT with P0=0, P1=0, and P2=0.
All anchors and the exact three dirty paths matched. It confirmed 12 test shapes
and 17 collected nodes: 11 deterministic current-source RED nodes and six
boundary-pass nodes. The byte-exact test prefix through line 3033 remained the
accepted RP11 oracle.

Cycle 1 findings are fully closed: both text sentinels cover all proportional
text methods and exact 256; nested strings cover ASCII/multibyte max+1 and exact
4096 bytes; nested and direct integer tests prove no `abs` and exact `-MAX` at
all sites. Public-path and mapping multi-sentinels are fixture-safe and prove
the complete selected ordering. The reviewer confirmed code/path expectations,
off-by-one behavior, monkeypatch restoration, reachable C1 fixtures, and no
false-green shape. It performed no executable, edit, Git, CAD, environment,
network, model, install, TK2, or external action. The one focused RP13 RED is
now authorized with source frozen.

## TK1-RP13 Focused RED — TK1-E029

Controller preflight matched artifact
`25cb62ee28d0f942bc32462025c012a606d7ad317a5b1f857d21d4f585945e40`
at 3371 lines, frozen source
`d25db0ba9daa89ba5d33a91344ca08a957f03c57a4d91eb86187b228223995da`
at 1719 lines, accepted tests
`0549002b19975ac3e46a04c80221cf975ca7f5009b6cfa4493aee236a5d869de`
at 3256 lines, branch/HEAD, and the exact three dirty paths.

The one authorized command,
`PYTHONPATH=src .venv/bin/pytest -q tests/test_task_state.py`, completed normally
with exit 1: 396 passed and exactly 11 failed in 5.92 seconds. Every failure was
one accepted RP13 sentinel: tuple copy; text strip; public path canonical scan;
public message strip; two C1 string parser nodes; the C1 integer parser; three
direct `abs` nodes; and mapping key snapshot. All six new exact-boundary nodes
and all 390 prior nodes passed. There was no setup, collection, import, process,
or transport failure. This is the genuine RP13 RED. Tests now freeze; only the
minimum `state.py` repair plus chronological artifact evidence is authorized,
with at most two focused GREEN cycles.

## TK1-RP13 Implementation and Gate Evidence — TK1-E030

Controller repaired only `src/vibecad/workflow/state.py`; accepted tests remained
byte-frozen. The repair adds the private 64-field mapping ceiling; checks public
error path/message and `_text` length before proportional work; checks exact
sequence length before tuple copying; applies the existing nested UTF-8 budget
and direct safe-integer bounds before C1 parsing; and removes all three state
`abs` range checks in favor of direct lower/upper comparisons.

GREEN cycle 1 passed all 407 focused tests in 3.13 seconds. Cumulative TK1/C1-C5
then passed 796 with 1 deselected. Ruff lint passed, while read-only format check
identified exactly one mechanical source delta: collapse one three-line direct
integer comparison to Ruff's single-line form. Controller applied only that
formatting change with `apply_patch`. Final allowed GREEN cycle 2 passed all 407
focused tests in 4.11 seconds.

Post-format final gates passed:

- cumulative TK1/C1-C5: 796 passed, 1 deselected in 8.46 seconds;
- six preserved mutation/round-trip readbacks: 6 passed in 0.27 seconds;
- two-file Ruff check: all checks passed;
- two-file Ruff format check: both files already formatted;
- fresh pure import: `task state pure import OK`;
- forbidden imported-module list: empty;
- broad Exception/BaseException search: empty;
- state `abs(` search: empty;
- `git diff --check`: exit 0;
- final source
  `f647333f6a80406cdf62ab4fa03b7c6ce3eeb3beaab71070e1213f5a92927c88`,
  1736 lines;
- tests remained
  `0549002b19975ac3e46a04c80221cf975ca7f5009b6cfa4493aee236a5d869de`,
  3256 lines;
- branch/HEAD and the exact three dirty paths remained unchanged.

The artifact before this evidence update was
`38eda56c9e8c1e3475a96291e19136501ca8be73a0d1ae174923b28a0ce6e0c4`,
3392 lines. No dependency/environment, CAD/network/model/install, Git mutation,
TK2, or external action occurred. Source and tests freeze for final review.

## Task Packet TK1-RP14 — Final Zero-Finding Review After Proportional Closure

TK-A03 authorizes one distinct final read-only review of the complete TK1
outcome through TK1-E030. The reviewer may inspect text, hashes, status, and
recorded evidence only; no edit, pytest/Python/Ruff/formatter, Git mutation,
CAD/environment/network/model/install action, TK2 work, or external side effect
is authorized.

The reviewer must replay TK-D01-TK-D32 and B01-B10 rather than relying on passing
tests. It must explicitly trace every RP12 finding through the RP13 source and
oracle, including invalid exact types, max/max+1, ASCII/multibyte, negative
integer lower bounds, mapping 64/65, sequence copy order, public error work
bounds, and nested parser order. It must also recheck all prior state-machine,
provenance, immutable serialization, alias/cycle/depth/node, error translation,
public export, and pure-import surfaces. Attempt concrete bypasses and inspect
tests for false positives. ACCEPT requires P0=0/P1=0/P2=0. Otherwise return
exact source/test lines, failure shape, and ledger mapping. Controller alone
owns acceptance evidence, staging, staged gates, commit, and push.

## TK1-RP14 Final Review Evidence — TK1-E031

The distinct read-only reviewer matched artifact
`65c193b2766d607f12d0bd97ca2709965f2693297e67be75bbf150d46d684067`
at 3451 lines, source
`f647333f6a80406cdf62ab4fa03b7c6ce3eeb3beaab71070e1213f5a92927c88`
at 1736 lines, tests
`0549002b19975ac3e46a04c80221cf975ca7f5009b6cfa4493aee236a5d869de`
at 3256 lines, branch/HEAD, and the exact three dirty paths. It returned REJECT
with P0=0, P1=2, and P2=0:

1. P1 / TK-D28+TK-D29 / B03+B05+B08: `_enum` calls Enum conversion on an
   unbounded exact string before a character ceiling. Public and raw paths can
   therefore perform proportional hashing, lookup, and ValueError construction
   before stable state rejection.
2. P1 / TK-D28 / B03+B05: the 64-field root cap bounds key count but the
   unknown-field `sorted` still compares up to 49 attacker-controlled keys with
   an unbounded shared prefix before `_error_pointer` can truncate one.

The reviewer confirmed every RP12/RP13 repair and all state-machine,
provenance, recovery, immutability, alias/cycle/depth/node, translation, export,
and pure-import surfaces otherwise close. Its separate state-matrix/provenance
review returned zero findings. No reviewer executed tests or modified files,
Git, CAD, environment, network, model, install, TK2, or external state.

## Task Packet TK1-RP15 — Enum and Root-Key Work Closure Under TK-A03

TK-A03 remains controlling. TK1-RP15 closes only TK1-E031 inside existing
TK-D28/TK-D29 and B03/B05/B08. No architecture, public state, dependency,
environment, external action, commit count, or later packet changes.

Selected behavior:

- Exact string enum inputs above the existing 256-character text ceiling fail
  with the existing INVALID_VALUE code, field path, and non-reflective message
  before Enum conversion. Exactly 256 characters still reach normal Enum
  semantics; this ceiling is character-based for both ASCII and multibyte text.
- Root mappings retain the 64-field cap and current lexicographic unknown-field
  selection for tokens of at most 256 characters. If any unknown token exceeds
  256 characters, select the first such token without sorting unknown content;
  `_error_pointer` then returns its existing stable truncated path. Required-
  field precedence and all short-key behavior remain unchanged.

Controller adds deterministic tests first. A distinct static reviewer must
return P0=0/P1=0/P2=0 before exactly one focused RED. Tests then freeze; only a
minimum `state.py` repair is allowed, with at most two focused GREEN cycles.
On GREEN rerun cumulative TK1/C1-C5, six readbacks, Ruff check/format,
pure/forbidden import, broad-exception, diff/hash/allowlist gates, and a distinct
zero-finding final review. No stage, commit, push, TK2, dependency/environment,
CAD, network, model, install, or external action is allowed before ACCEPT.

## TK1-RP15 Test-Design Candidate — TK1-E032

Controller appended six independently collected pytest nodes across four test
shapes. Tests changed from
`0549002b19975ac3e46a04c80221cf975ca7f5009b6cfa4493aee236a5d869de` to
`fbba54ae92a571fdd18906bcd905db10836a2c85f2b90e0e9593fc5b82a4a0e4`,
3319 lines. Source remained frozen at
`f647333f6a80406cdf62ab4fa03b7c6ce3eeb3beaab71070e1213f5a92927c88`,
1736 lines. The artifact before this evidence update was
`3a4c1c4a779d4e4727c279b3f06db5d9a4598fd78f84d6fe19c6505d000a9c45`,
3502 lines.

Two ASCII/multibyte max+1 enum nodes replace `TaskStatus._missing_` with an
exploding conversion sentinel; two exact-256 nodes return CREATED from that
same genuine Enum conversion boundary. A reachable 64-field TaskRun mapping
uses many overlong shared-prefix unknown keys and an exploding `sorted`, while
the final node preserves lexicographic selection for ordinary short unknown
keys. Static prediction is three current-source sentinel failures and three
boundary passes. No executable, source edit, formatter, Git, CAD, environment,
network, model, install, TK2, or external action occurred. Static
P0=0/P1=0/P2=0 ACCEPT is required before RED.

## TK1-RP15 Static Test Review — TK1-E033

The distinct read-only reviewer returned ACCEPT with P0=0, P1=0, and P2=0.
All anchors, the byte-exact prior test prefix, branch/HEAD, and the exact three
dirty paths matched. It confirmed four shapes and six nodes with static current-
source prediction of three sentinel failures and three boundary passes.

The reviewer verified genuine EnumType `_missing_` dispatch, classmethod
signature/restoration, exact ASCII/multibyte 256/257 character behavior, the
reachable exact-64 TaskRun mapping, long-token truncation path, lexicographic
short-key preservation, and all expected codes/paths. No false-positive,
fixture, syntax, name, or collection blocker exists. It performed no executable,
edit, Git, CAD, environment, network, model, install, TK2, or external action.
The one focused RP15 RED is authorized with source frozen.

## TK1-RP15 Focused RED — TK1-E034

Controller preflight matched artifact
`bac538dee915555e6250c7c79466d9acaec0fd77dd6241d19fbc46b6eec4c44a`
at 3539 lines, frozen source
`f647333f6a80406cdf62ab4fa03b7c6ce3eeb3beaab71070e1213f5a92927c88`
at 1736 lines, accepted tests
`fbba54ae92a571fdd18906bcd905db10836a2c85f2b90e0e9593fc5b82a4a0e4`
at 3319 lines, branch/HEAD, and the exact three dirty paths.

The single authorized focused command completed normally with exit 1: 410
passed and exactly three failed in 7.66 seconds. Both max+1 enum nodes reached
their `_missing_` conversion sentinel and the exact-64 long-key mapping reached
its `sorted` sentinel. Both exact-256 enum nodes, the short-key lexicographic
node, and all 407 prior nodes passed. No setup, collection, process, import, or
transport failure occurred. Tests now freeze; only the minimum `state.py`
repair and chronological evidence updates are authorized, with at most two
focused GREEN cycles.

## TK1-RP7 Formatting Migration Evidence — TK1-E012

Controller preflight matched branch/HEAD, the three dirty paths, accepted test
hash 5507c54593e432a0f53e306f471e40d4edc02d3e0dbe850919a297e22d31e301,
frozen source 8a2d2bf65c015258e455d0e7efb221c5da2b16ff582907c2a3ac179a06e4cd22,
and starting artifact
6669af0dbba751d5da97e4071db71f45224270aa8cb66038d179f907e4605731.

Read-only Ruff diffs identified exactly four mechanical regions:

- alphabetic import ordering for MAX_VERDICT_EVIDENCE and TaskRun/
  TaskStepRecord/TaskTransitionRecord;
- one three-line generator expression collapsed to Ruff's single-line form;
- one transition assertion parenthesized and line-wrapped;
- two long nested verification-report assignments collapsed to single lines.

The controller applied only those exact changes through apply_patch. No
assertion, literal, identifier, decorator, parameter, fixture, or control flow
changed. The migrated test hash is
6fcb989498efb3be05531178c684822a61c0d36878438bcef934d8246de1f620.
Source remained exactly
8a2d2bf65c015258e455d0e7efb221c5da2b16ff582907c2a3ac179a06e4cd22.

Post-format evidence:

- focused test_task_state.py: 362 passed in 11.26 seconds;
- cumulative TK1/C1–C5: 751 passed, 1 deselected in 11.90 seconds;
- six named mutation/round-trip tests: 6 passed in 1.16 seconds;
- two-file Ruff check: all checks passed;
- two-file Ruff format check: both files already formatted;
- fresh pure import: `task state pure import OK`;
- forbidden imported-module list: empty;
- git diff --check: exit 0;
- dirty paths remain exactly artifact, state.py, and test_task_state.py;
- branch/HEAD remains
  codex/task-kernel-phase2@bf8967077addfbd8002d6289b0eb925f3f55b638.

No source, product behavior, environment, dependency, CAD, network, model,
stage, commit, push, or TK2 action occurred.

## Task Packet TK1-RP8 — Active Final Deep Acceptance Review Under TK-A03

### 1. Authorization

TK-A03 authorizes a final read-only acceptance review of TK1-B01–TK1-B10 after
TK1-E011 production closure and TK1-E012 mechanical oracle migration. The
reviewer has no mutation, execution, Git, environment, CAD, network, model, or
publication authority.

### 2. Workspace anchor

- Repository: /Users/wangtao/Documents/DevProject/vibecad.
- Branch/HEAD:
  codex/task-kernel-phase2@bf8967077addfbd8002d6289b0eb925f3f55b638.
- Review source:
  8a2d2bf65c015258e455d0e7efb221c5da2b16ff582907c2a3ac179a06e4cd22.
- Review tests:
  6fcb989498efb3be05531178c684822a61c0d36878438bcef934d8246de1f620.
- Reviewer must compute and report the current artifact hash.
- Dirty paths must remain exactly the three approved untracked files.

### 3. Context

The reviewer must independently replay the findings and evidence in TK1-E002,
TK1-E004, TK1-E009, TK1-E010, TK1-E011, and TK1-E012. Passing tests are
necessary but insufficient: implementation must be fail-closed, bounded,
immutable, provider-neutral, pure on import, and faithful to candidate,
AcceptanceSpec, report, artifact, error, and transition provenance.

### 4. Steps and gates

1. Reconfirm branch/HEAD, hashes, dirty paths, public exports, and no unrelated
   file drift using read-only inspection only.
2. Review state.py line by line against TK-D01–TK-D32 and the full B01–B10
   finding ledger. Trace validation order and stable error code/path behavior.
3. Review the formatted tests for semantic identity with TK1-E012 and adequate
   adversarial coverage; explicitly verify every earlier P0/P1/P2 finding.
4. Inspect JSON node/depth/alias/cycle handling, exact container preflight,
   safe numeric validation, bounded error rendering, and native-exception
   containment for bypasses not exercised by obvious happy paths.
5. Inspect complete durable state/reconciliation history for forged candidate,
   report, AcceptanceSpec, artifact, step, transition, and last_error cases.
6. Return ACCEPT only at P0=0/P1=0/P2=0. Otherwise return REJECT with exact
   severity, source/test line anchors, exploit or failure shape, and mapping to
   B01–B10. Do not disposition a finding by merely citing passing tests.
7. Confirm no file edit, pytest/Python/Ruff/formatter execution, Git mutation,
   CAD/network/model/install action, or external side effect occurred.

### 5. Execution discipline

- Distinct read-only reviewer; no delegation to the implementation worker.
- Text/hash/status inspection only. Do not run executable validation already
  preserved by the controller.
- Stop and REJECT on any P0/P1/P2, hash mismatch, out-of-allowlist drift,
  semantic test-format change, hidden broad exception, or material boundary.
- Never edit, stage, commit, push, reset, amend, install, or start TK2.

### 6. Delivery boundary

Deliver only a finding ledger and ACCEPT/REJECT verdict. Controller retains all
artifact, repair, Git, push, and TK2 authority.

### 7. Final report

Return exact hashes/line counts; P0/P1/P2; prior-finding replay; new adversarial
analysis; formatting semantic-identity finding; residual risks; prohibited-
action confirmation; and branch/HEAD/status.

## Recovery Snapshot — TK-S012

### 1. Completed milestones

- TK1 source and accepted oracle pass focused/cumulative/readback/static/import
  gates after mechanical formatting migration.
- Source and tests are frozen at TK1-RP8 review hashes.
- No Git publication or TK2 action has occurred.

### 2. Next steps

1. Distinct reviewer returns zero-finding ACCEPT or an exact internal repair
   ledger.
2. On ACCEPT, controller records final evidence and performs staged gates.
3. Commit/push exactly TK1, then activate TK2 under the approved sequence.

### 3. Approved decisions

- TK-R1/TK-A02 and TK-R2/TK-A03 remain active.
- Final review is internal acceptance, not a user approval point.
- P0/P1/P2 must all be zero before Git mutation.

### 4. Execution discipline

- Source/tests are read-only throughout review.
- Preserve every review finding; do not hide or auto-disposition it.
- No external/environment/CAD/model/Git action before ACCEPT.

## TK1-RP17 Minimum Repair and GREEN Evidence — TK1-E040

At 2026-07-18T00:32:36Z the controller matched branch/HEAD, the exact three
approved untracked paths, pre-entry artifact
`8c70c3130e5d637195839d3cf078a3da4d3974b3d157ca6c3ad5acb63d0f901c`
at 3931 lines, repaired source
`d11ca0830ee6069cc5080c58d60c61fc5e7c28083d074430ec5c77af614dd294`
at 1743 lines, and final tests
`aaf7e43f5f1a9fe7f96e997d83fe9564fb465ef8599cb51b9210b524749a1ae1`
at 3377 lines. The first 3319 test lines remain byte-exact at
`fbba54ae92a571fdd18906bcd905db10836a2c85f2b90e0e9593fc5b82a4a0e4`.

The minimum repair replaces the public error-code and common enum-member
`isinstance` checks with exact type identity and makes three directly
corresponding internal assertions exact. No enum, state, transition, mapping,
string normalization, error code/path/message, public signature, dependency,
or other behavior changed.

Objective gates:

- Focused GREEN 1: exit 0, 418 passed in 4.62 seconds.
- Cumulative TK1/C1-C5 after GREEN 1: exit 0, 807 passed and 1 deselected in
  10.88 seconds.
- Six declared mutation/round-trip readbacks after GREEN 1: exit 0, 6 passed
  in 1.29 seconds.
- Ruff check passed, but format check requested one exact mechanical collapse
  of the new test function signature. Controller applied only that diff; no
  assertion, fixture, parameter, literal, identifier, or control flow changed.
- Focused GREEN 2 after formatting: exit 0, 418 passed in 19.53 seconds.
- Final cumulative TK1/C1-C5: exit 0, 807 passed and 1 deselected in 6.94
  seconds; final six readbacks: exit 0, 6 passed in 1.41 seconds.
- Final Ruff check: all checks passed; format check: both files already
  formatted.
- Fresh pure import: exit 0, `task state pure import OK`; forbidden imported-
  module list was empty.
- Broad `BaseException`/`Exception`, `abs(`, and enum-class `isinstance`
  searches returned no source/test matches; `git diff --check` exited 0.
- Dirty and untracked paths remained exactly the approved three files at the
  unchanged branch/HEAD.

No dependency, environment, CAD, network, model, install, Git mutation, TK2,
or external action occurred. Source and tests freeze at the final hashes above
for complete final review.

## Task Packet TK1-RP18 — Final Zero-Finding TK1 Review Under TK-A03

### 1. Authorization

TK-R1/TK-A02 and the more specific TK-R2/TK-A03 authorize one distinct final
read-only review of the complete TK1 result through TK1-E040. This packet
inherits all higher-priority system, developer, and user instructions,
applicable directory-scoped instructions, the approved allowlist, and the
current host permission model and sandbox. The Skill, artifact, and packet
cannot grant or expand permissions, elevate authority, or bypass that model or
sandbox. Do not request the same approval again.

### 2. Workspace anchor

- Repository: `/Users/wangtao/Documents/DevProject/vibecad`.
- Branch/HEAD:
  `codex/task-kernel-phase2@bf8967077addfbd8002d6289b0eb925f3f55b638`.
- Source: `d11ca0830ee6069cc5080c58d60c61fc5e7c28083d074430ec5c77af614dd294`,
  1743 lines.
- Tests: `aaf7e43f5f1a9fe7f96e997d83fe9564fb465ef8599cb51b9210b524749a1ae1`,
  3377 lines, with the accepted 3319-line prefix hash above.
- Controller supplies the post-entry artifact hash and line count with the
  live assignment. Dirty paths must remain exactly the three approved
  untracked files; no VibeCAD-scoped `AGENTS.md` or `CLAUDE.md` was observed.
- Current permissions and sandbox remain binding. Review is read-only
  text/hash/status inspection; all mutation and external action is prohibited.

### 3. Context

TK1 must remain fail-closed, bounded, immutable, provider-neutral, pure on
import, and fully provenance-faithful. RP16 found the final `__class__` proxy
identity bypass. RP17 produced exactly five predicted failures and closed them
with exact identity while retaining genuine enum members and strings. Passing
tests and prior reviews are evidence, not substitutes for this final replay.

### 4. Steps and gates

1. Reconfirm branch/HEAD, exact hashes/lines, dirty paths, public exports, and
   absence of unrelated drift using read-only inspection.
2. Re-evaluate public `TaskStateError.code`, `_enum`, all four enum types, raw
   and public callers, genuine members/aliases/strings, invalid exact types,
   ASCII/multibyte 256/257, native hash/property/Enum exceptions, and stable
   error codes/paths. Verify proxy objects cannot persist or reach later work.
3. Re-evaluate root mapping required/unknown precedence, 64/65, ordinary-key
   lexicographic behavior, overlong shared prefixes, truncation, and work order.
4. Replay every prior P0/P1/P2 and TK1-B01-TK1-B10: full state/provenance and
   reconciliation matrices; immutable serialization; exact sequence/mapping
   preflight; alias/cycle/depth/node; text/key/error work bounds; negative and
   oversized numbers; narrow error translation; exports and pure imports.
   Attempt concrete bypasses rather than relying on test names.
5. Review all new proxy oracles and the mechanical format change for
   reachability, restoration, false positives, and semantic identity.
6. Return ACCEPT only for P0=0/P1=0/P2=0. Otherwise return REJECT with exact
   severity, source/test lines, failure shape, and B/decision mapping.
7. Confirm no edit, pytest/Python/Ruff/formatter execution, Git mutation,
   CAD/environment/network/model/install, TK2, or external side effect.

### 5. Execution discipline

- Delegation: `spawn-send-wait`; model tier: `deep`; process:
  `native-session-poll` if a controllable long command existed, though this
  packet permits no executable validation.
- Reviewer is distinct from the implementation worker and may perform only
  read-only text/hash/status inspection. Stop on any hash, allowlist, or scope
  mismatch; any P0/P1/P2 is a gate red owned by the controller.
- No reviewer edit, self-repair, self-disposition, stage, commit, push, or TK2.

### 6. Delivery boundary

Deliver only an evidence-backed finding ledger and ACCEPT/REJECT verdict.
Controller retains acceptance evidence, staging, staged gates, commit, push,
recovery, and TK2 authority.

### 7. Final report

Return exact hashes/lines, branch/HEAD/status, P0/P1/P2, explicit RP16/RP17
closure, complete prior-finding/state/provenance replay, new adversarial
analysis, test-format semantic identity, residual risks, prohibited-action
confirmation, and final verdict.

## Recovery Snapshot — TK-S018

### 1. Completed milestones

- RP17 static review and exactly predicted five-failure RED are preserved.
- The exact-identity repair is green through focused, cumulative, readback,
  Ruff, pure-import, broad-search, diff, hash, and allowlist gates at the frozen
  hashes in TK1-E040.
- No Git publication or TK2 action has occurred.

### 2. Next steps

1. A distinct reviewer executes TK1-RP18 and returns zero-finding ACCEPT or an
   exact internal repair ledger.
2. On ACCEPT, controller records review evidence, stages only the three
   approved files, reruns staged gates, commits and pushes TK1, and verifies
   upstream equality and clean state. On REJECT, preserve and route a bounded
   test-first repair without duplicate user approval.
3. After accepted TK1 publication, activate TK2 under the approved sequence.

### 3. Approved decisions

- TK-R1/TK-A02 and TK-R2/TK-A03 remain active; TK-R2 controls TK1 closure.
- Final review and deterministic repair are internal acceptance points, not
  user approval points. P0/P1/P2 must all be zero before Git mutation.

### 4. Execution discipline

- Capability profile remains `native-plan`, `spawn-send-wait`,
  `repo-artifact`, and `native-session-poll` on the Codex adapter.
- Preserve exact three-file allowlist, frozen source/tests, all RED/review
  evidence, and approved breakers. Controller alone stages, commits, pushes,
  records acceptance, and activates TK2.

## TK1-RP18 Final Acceptance Evidence — TK1-E041

The distinct final read-only reviewer returned ACCEPT with P0=0, P1=0, and
P2=0. It matched artifact
`b57e6249b3a90afe1a9334835f58addff341af0d9a3a925f277e9ee0d876b855`
at 4089 lines, source
`d11ca0830ee6069cc5080c58d60c61fc5e7c28083d074430ec5c77af614dd294`
at 1743 lines, tests
`aaf7e43f5f1a9fe7f96e997d83fe9564fb465ef8599cb51b9210b524749a1ae1`
at 3377 lines, the accepted 3319-line test prefix, branch/HEAD, and exactly the
three approved untracked paths with no drift.

The reviewer explicitly closed both RP16 findings: public error code and all
common enum boundaries now reject `__class__` proxies before hash, value,
property, storage, or serialization work while preserving genuine members,
aliases, and exact strings. It rechecked RP15 root mapping bounds and ordinary-
key behavior, replayed all B01-B10 and prior P0/P1/P2 findings, and found no new
state-machine, provenance, reconciliation, immutability, serialization,
container, text/key/error, numeric, translation, export, or import issue. The
RP17 formatter change is mechanically and semantically identical.

Only the previously accepted Stage 3 raw-ingress/C1 preconstructed-object and
later service/CAD isolation residuals remain; none is a TK1 finding. The
reviewer used only read-only text/hash/status inspection and performed no edit,
executable validation, Git mutation, CAD/environment/network/model/install,
TK2, or external action. TK1 is authorized to enter exact staging and staged
gates; controller retains all Git and publication authority.

## Recovery Snapshot — TK-S019

### 1. Completed milestones

- TK1-RP18 independently accepted the final frozen source/tests at
  P0=0/P1=0/P2=0 after the complete historical replay.
- Focused, cumulative, readback, Ruff, pure-import, search, diff, hash, and
  allowlist gates are green. No Git publication or TK2 action has occurred.

### 2. Next steps

1. Stage exactly the artifact, `state.py`, and `test_task_state.py`; reject any
   staged or unstaged path outside this allowlist.
2. Run the staged name/diff gates and declared focused, cumulative, readback,
   Ruff, pure-import, and search checks against the staged content.
3. Commit with `feat(workflow): define durable task-run state contracts`, push
   non-force to `origin/codex/task-kernel-phase2`, verify `HEAD == @{upstream}`
   and a clean workspace, then append closeout and activate TK2.

### 3. Approved decisions

- TK-R1/TK-A02 and TK-R2/TK-A03 remain active and authorize this exact TK1
  publication. No pull request, force push, main-branch push, or scope change is
  authorized.
- RP18 is the required final zero-finding internal acceptance.

### 4. Execution discipline

- Capability profile remains `native-plan`, `spawn-send-wait`,
  `repo-artifact`, and `native-session-poll` on the Codex adapter.
- Use named-path staging only, preserve all evidence, push immediately after
  the accepted commit, and stop on any staged drift, gate red, process
  ambiguity, or publication mismatch.

## TK1 Staged Gate Evidence — TK1-E042

Controller staged exactly the three approved paths by name. Before this
chronological evidence append, the index contained no unstaged difference and
had these exact blobs:

- artifact
  `a0a2bd2a98ee24d57674e7c144dcec6f1e881e63b0ebc86cf3b58b2989350282`;
- source
  `d11ca0830ee6069cc5080c58d60c61fc5e7c28083d074430ec5c77af614dd294`;
- tests
  `aaf7e43f5f1a9fe7f96e997d83fe9564fb465ef8599cb51b9210b524749a1ae1`.

The cached name list was exactly the artifact, `state.py`, and
`test_task_state.py`; cached diff check exited 0. Staged-content gates then
completed normally:

- focused `test_task_state.py`: exit 0, 418 passed in 4.58 seconds;
- cumulative TK1/C1-C5: exit 0, 807 passed and 1 deselected in 2.89 seconds;
- six mutation/round-trip readbacks: exit 0, 6 passed in 0.18 seconds;
- Ruff check: all checks passed; format check: both files already formatted;
- fresh pure import: exit 0, forbidden imported-module list empty;
- broad exception, `abs(`, and enum-class `isinstance` searches: no relevant
  matches;
- branch/HEAD remained unchanged and status showed exactly the three named
  additions in the index.

This append is documentation-only G0 evidence. Controller must restage only the
artifact, recheck the final cached names/diff and an empty unstaged diff, then
create the approved commit
`feat(workflow): define durable task-run state contracts` and push it non-force.

## TK1 Commit and Push Closeout — TK1-E043

The controller restaged only the artifact after TK1-E042. Final cached paths
were exactly the approved three files, cached diff check exited 0, and no
unstaged difference remained. Final index hashes were artifact
`a0c248d8653189edf2aac383dcfad2beb4b13dc0fc0e86f1d8865d74847adfe4`,
source `d11ca0830ee6069cc5080c58d60c61fc5e7c28083d074430ec5c77af614dd294`,
and tests `aaf7e43f5f1a9fe7f96e997d83fe9564fb465ef8599cb51b9210b524749a1ae1`.

Commit `1ef1d5df4aaa67e43ea0041a0404cdb9b3388e4a` was created with the approved
message `feat(workflow): define durable task-run state contracts`. It contains
exactly the artifact, `state.py`, and `test_task_state.py`. Non-force push to
`origin/codex/task-kernel-phase2` succeeded and established the upstream.
Local HEAD and `@{upstream}` both resolved to the exact commit, and the
workspace was clean. The remote repeated its existing redirect notice to
`https://github.com/wangtao9090/VibeCAD.git`; residual TK-R19 remains open and
the controller did not rewrite the user remote. TK1 is completed and pushed;
commit budget usage is 1 of 9.

## Task Packet TK2-P1 — Exclusive Resource Leases Under TK-A02/TK-A03

### 1. Authorization

TK-R1/TK-A02 approved TK2, decisions TK-D05-TK-D07 and TK-D24, the exact
allowlist, gates, budgets, non-force push, and recovery discipline. TK-R2/
TK-A03 remains active where it governs Stage C fail-closed repair discipline.
This packet inherits all higher-priority system, developer, and user
instructions, applicable directory-scoped instructions, the approved
allowlist, and the current host permission model and sandbox. The Skill,
artifact, and packet cannot grant or expand permissions, elevate authority, or
bypass that model or sandbox. Do not request the same approval again.

### 2. Workspace anchor

- Repository: `/Users/wangtao/Documents/DevProject/vibecad`.
- Branch/HEAD/upstream:
  `codex/task-kernel-phase2@1ef1d5df4aaa67e43ea0041a0404cdb9b3388e4a`,
  equal to `origin/codex/task-kernel-phase2` before this append.
- TK2 write allowlist is exactly
  `docs/orchestrated/vibecad-task-kernel-phase2.md`,
  `src/vibecad/workflow/lease.py`, and `tests/test_workflow_lease.py`.
- All other source/tests, dependencies, configuration, CAD, environment,
  network, install, model, server, public MCP, publication, and TK3 scope is
  prohibited. No VibeCAD-scoped `AGENTS.md` or `CLAUDE.md` was observed.
- Current host permissions and sandbox remain binding.

### 3. Context

TK2 provides the process-local and operating-system exclusion primitive used by
later project writes. The file descriptor and OS advisory lock are
authoritative; owner text is diagnostic only. Resource identifiers never
select paths directly: a stable SHA-256 key under the configured lock root is
used. A lease has one manager-issued owner token and one exact release; wrong-
owner, double-release, unsafe path, unsupported platform, invalid input, and
contention failures use stable typed errors. There is no TTL, stale-lock
deletion, PID ownership claim, lock stealing, or unlocked fallback.

The configured root is an explicit trusted-local boundary. The implementation
must reject symlink/non-directory roots and symlink/non-regular lock entries,
use non-following ordinary-file opens where supported, and never claim network-
filesystem durability. POSIX behavior must be exercised on this host. Windows
adapter logic may be statically and deterministically tested, but live Windows
semantics remain unclaimed unless observed on Windows.

### 4. Steps and gates

1. Reconfirm the clean pushed TK1 anchor and run the unchanged full baseline.
   Stop on an unexplained failure or any path outside the TK2 allowlist.
2. Collect bounded independent read-only design/test reviews for cross-manager
   thread exclusion, OS descriptor ownership, path safety, owner-token exact
   release, POSIX subprocess behavior, Windows fail-closed adaptation, and
   deterministic tests. Controller resolves them inside this approved design.
3. Controller creates `tests/test_workflow_lease.py` first. A distinct static
   review must return P0=P1=P2=0 before one genuine missing-module RED. The RED
   must be collection failure solely because `vibecad.workflow.lease` is absent.
4. Freeze tests and implement only `src/vibecad/workflow/lease.py`. Provide
   `ResourceLeaseManager`, `ResourceLease`, `ProjectWriteLease`, stable public
   error codes/types, SHA-256 resource keys, manager-issued owner tokens, exact
   release and context management. Coordinate distinct manager instances in
   one process and hold a nonblocking/bounded OS advisory lock for the lease
   lifetime. Never reclaim by elapsed time.
5. GREEN must cover two-manager threads, subprocess contention, process-exit
   release, wrong owner, double release, exception context release, different
   resources, symlink/non-regular paths, invalid/bounded identifiers, and the
   POSIX adapter. Windows branches must be deterministic and fail closed when
   unobservable; record TK-R07 rather than claiming a live Windows pass.
6. Use at most two bounded GREEN attempts. Then run full compatibility,
   focused tests, Ruff check/format, pure import, diff/hash/allowlist, and a
   distinct complete read-only review. ACCEPT requires P0=P1=P2=0.
7. Controller stages only the three TK2 files, runs staged gates, commits with
   `feat(workflow): coordinate exclusive resource leases`, pushes non-force,
   and verifies HEAD/upstream equality and a clean workspace before TK3.

### 5. Execution discipline

- Delegation: `spawn-send-wait`; model tier: `standard` for implementation and
  `deep` for adversarial review; process: `native-session-poll` for any returned
  live command session.
- Controller alone edits the artifact and tests; production source is written
  only after an accepted RED and then tests freeze. Read-only design reviews
  may run in parallel; all writes and pytest runs are serialized.
- Acquisition tests and synchronous paths are nonblocking or bounded to at
  most 5 seconds. Stop on deadlock, ambiguous process state, unsafe cleanup,
  unexpected RED, second unsuccessful GREEN, path drift, or any campaign
  breaker. Test cleanup may remove only its own pytest temporary paths.

### 6. Delivery boundary

Complete only the lease primitive, its tests, evidence, review, named-file
commit, and non-force push. Do not integrate TaskStore/TaskService, touch CAD,
change dependencies, expose MCP, rewrite the remote, create a PR, or start TK3
before accepted TK2 publication.

### 7. Final report

Return resolved API/semantics, exact hashes and line counts, baseline and
RED/GREEN evidence, platform observations and unclaimed branches, contention/
cleanup evidence, independent review P0/P1/P2, residuals, commit/push state,
prohibited-action confirmation, and final workspace anchor.

## Recovery Snapshot — TK-S020

### 1. Completed milestones

- TK1 is independently accepted, committed at
  `1ef1d5df4aaa67e43ea0041a0404cdb9b3388e4a`, pushed, upstream-equal, and was
  clean before this TK2 artifact append.
- TK2-P1 is activated under existing approval; no TK2 source/test exists yet.

### 2. Next steps

1. Run the unchanged full baseline and collect the bounded read-only TK2 design
   reviews. Stop on unexplained baseline red or scope drift.
2. Controller records the resolved test contract, writes only the TK2 test
   file, obtains static zero-finding acceptance, and runs one missing-module RED.
3. On the exact RED, freeze tests, implement only `lease.py`, run bounded GREEN
   and full gates, then assign a distinct final review.

### 3. Approved decisions

- TK-R1/TK-A02, TK-R2/TK-A03, TK-D05-TK-D07, and TK-D24 remain active.
- TK2 is an internal deterministic lease primitive; Windows live behavior is
  unclaimed on this POSIX host and is recorded under TK-R07 unless later
  observed on Windows.

### 4. Execution discipline

- Capability profile remains `native-plan`, `spawn-send-wait`,
  `repo-artifact`, and `native-session-poll` on the Codex adapter.
- Exact TK2 allowlist: artifact, `lease.py`, and `test_workflow_lease.py`.
- Preserve test-first ordering, the 5-second lock budget, named-file staging,
  immediate non-force push, and every campaign circuit breaker.

## TK2 Baseline Evidence — TK2-E001

At the clean pushed TK1 anchor
`1ef1d5df4aaa67e43ea0041a0404cdb9b3388e4a`, with artifact
`dc9578ffd5cf97ffb2732ca979097aa2444b2fc8216ac62ef1b332834c6fcde2`
at 4336 lines as the only modified path, the unchanged full command
`PYTHONPATH=src .venv/bin/pytest -q` completed normally: exit 0, 1300 passed
and 81 deselected in 52.34 seconds. No source/test, dependency, environment,
CAD, network, model, install, Git mutation, TK3, or external action occurred.

## TK2 Resolved Lease and Test Contract — TK2-E002

Two bounded independent read-only reviews completed for filesystem/platform
and deterministic-test risks. A third API review exceeded its bounded wait and
was stopped without a report; silence was not classified as a finding or tool
failure. The controller independently resolved the public contract inside
TK-D05-TK-D07 and adopts every P1/P2 item from the two completed reviews before
creating tests.

### Public contract

- `LeaseRootTrust` has one accepted value, `TRUSTED_LOCAL`; manager construction
  without that exact value fails with `UNTRUSTED_ROOT` before storage work.
- `LeaseErrorCode` is the exact closed set `INVALID_RESOURCE`, `INVALID_OWNER`,
  `UNTRUSTED_ROOT`, `UNSAFE_ROOT`, `UNSAFE_LOCK_ENTRY`, `CONTENDED`,
  `WRONG_OWNER`, `ALREADY_RELEASED`, `WRONG_PROCESS`, `INVALID_LEASE`,
  `UNSUPPORTED_PLATFORM`, `LOCK_UNAVAILABLE`, and `IO_ERROR`.
- `LeaseError` uses fixed bounded non-reflective messages and may expose only a
  validated 64-lowercase-hex resource key, never raw root/resource/token text.
- `ResourceLeaseManager(lock_root, *, trust)` exposes nonblocking
  `acquire(resource_id)`, `acquire_project_write(project_id)`, and
  `release(lease, *, owner_token)`.
- `ResourceLease` exposes immutable `resource_key`, `owner_token`, and
  `released`, exact-owner `release(*, owner_token)`, and context management.
  `ProjectWriteLease` is the exact project-specific lease type and exposes the
  canonical project identifier. Wrong manager/forged lease is `INVALID_LEASE`.
- Resource input is exact bounded printable single-line Unicode; project input
  is exact `project_<32-lowercase-hex>`. The lock key is lowercase SHA-256 of
  `b"vibecad-resource-lease-v1\0" + resource_id.encode("utf-8")`; the only
  selected entry is `<resource_key>.lock`. Owner tokens are manager-issued
  64-lowercase-hex values and never select a path.

### Exclusion and filesystem contract

- The process registry key is `(platform, root st_dev, root st_ino,
  resource_key)`, not a pathname. It reserves before any second same-process
  adapter call and permits different roots/resources. No reentrant acquisition.
- The root is a pre-existing absolute private directory. POSIX opens every
  component with directory FD, `O_DIRECTORY`, `O_NOFOLLOW`, and `O_CLOEXEC`,
  requires current-euid ownership and mode 0700, fsync support, and rechecks the
  pinned root identity on every acquisition. Any missing primitive fails closed.
- Entries use 0600, `O_RDWR|O_NOFOLLOW|O_CLOEXEC|O_NONBLOCK`, no truncate or
  append, exact regular-file/current-euid/mode/nlink-one checks, and matching
  directory-entry versus FD identity before and after lock acquisition.
  Existing regular contents, including PID/time-like text, are non-authoritative.
- A lock entry is persistent: release, context failure, child exit, and all
  cleanup paths never unlink, rename, replace, reclaim, or change its inode.
  Symlink/non-regular/hard-linked/unsafe-mode entries fail without target damage.
- POSIX uses `flock(LOCK_EX|LOCK_NB)` and exact `LOCK_UN`; only
  EACCES/EAGAIN is contention, ENOSYS/ENOTSUP is lock unavailable, and other
  native failures become IO error. Descriptor and process reservation lifetime
  are independently tested.
- Manager/lease objects record creator PID. File descriptors are non-inheritable;
  at-fork child handling closes inherited active lock descriptors and resets
  process registry state. An inherited object fails `WRONG_PROCESS` before
  owner/state/FD work.
- Release validates creator PID, exact lease/manager, owner token, and state
  before adapter or FD work. It performs unlock, close, then registry removal;
  wrong-owner/double-release paths perform none of those operations.
- Windows byte-range adapter calls seek-to-zero then nonblocking one-byte lock,
  and seek-to-zero then exact one-byte unlock. It is injectable only through a
  private test seam. Production `win32` and unknown selectors return
  `UNSUPPORTED_PLATFORM` until a live Windows gate exists; no POSIX or unlocked
  fallback is permitted.

### Deterministic test contract

- Target approximately 48 collected nodes. Independently prove the process
  registry with an always-grant recording adapter and the OS layer with a live
  POSIX subprocess holding a descriptor.
- Thread tests use `Event` handshakes. Subprocess tests use unbuffered JSON-line
  stdin/stdout handshakes, explicit `PYTHONPATH=src`, child module-path proof,
  and one monotonic deadline of at most 5 seconds. No sleeps, timing inference,
  marker files, mtime/PID ownership, or unreachable notification waits.
- Process-exit tests first prove contention, then use child `os._exit(0)`, wait
  for real termination, and reacquire the same persistent inode. Fixture
  cleanup kills/waits only its own live children and closes its pipes.
- Tests cover public exports/pure import; hash/token/input boundaries; root and
  entry symlink/non-regular/hardlink/mode/identity cases; same/different
  manager/root/resource; adapter failure rollback; wrong-owner/manager/double
  release; normal/exception context; persistent inode; POSIX flag/error/live
  contention/fork/exit; Windows fake call order/error mapping; and fail-closed
  platform selection.
- Windows live mutual exclusion, reparse/ACL behavior, handle inheritance,
  process-exit release, and directory durability remain unclaimed as TK-R07.
  Static adapter success cannot close that residual.

Controller now owns the test-file candidate. A distinct static review must
confirm these resolved items and P0=P1=P2=0 before the one missing-module RED.

## TK2 Test-Design Candidate — TK2-E003

Controller created only `tests/test_workflow_lease.py`; no lease source exists.
The candidate hash is
`5130c4ac32313b78555531a3be9e1ddc5df5f5a9caa3402a005aaf9553d94b51`
at 1061 lines. It has 48 test shapes and a static 75-node expansion. The
pre-entry artifact was
`139c7c8577e2d3f8c083c4d7745f3249486ecac536333d7e82eaff7623ff1f82`
at 4436 lines. Branch/HEAD/upstream remained the pushed TK1 commit; dirty paths
were exactly the artifact and new TK2 test file.

The candidate binds the exact public/private seam, error codes, root/resource/
token limits, two-layer exclusion, persistent inode, root/entry safety,
same/different resource behavior, release ownership, adapter rollback, POSIX
flags/errors/live contention/process exit/fork handling, Windows byte-range
adapter/error calls, fail-closed selector, pure import, and absence of TTL/
stale deletion. Thread and subprocess handshakes use `Event`, pipe/JSON lines,
real process termination, and one five-second deadline without sleep or marker
files. Tests own and clean only pytest temporary paths and their child processes.

The file imports `vibecad.workflow.lease` directly at module scope. Static RED
prediction is one collection error caused solely by missing module
`vibecad.workflow.lease`; no production/source, executable, formatter, Git,
CAD, environment, network, model, install, TK3, or external action occurred.

## Task Packet TK2-RP1 — Static Lease Test Review

### 1. Authorization

TK-R1/TK-A02 and TK-R2/TK-A03 authorize this distinct read-only static review
inside TK2-P1. It inherits all higher-priority instructions, the current
permission model/sandbox, and the exact allowlist. The Skill, artifact, and
packet do not expand authority. Do not request duplicate approval.

### 2. Workspace anchor

- Repository: `/Users/wangtao/Documents/DevProject/vibecad`.
- Branch/HEAD/upstream:
  `codex/task-kernel-phase2@1ef1d5df4aaa67e43ea0041a0404cdb9b3388e4a`.
- Tests:
  `5130c4ac32313b78555531a3be9e1ddc5df5f5a9caa3402a005aaf9553d94b51`,
  1061 lines; lease source must remain absent.
- Controller supplies the post-packet artifact hash/lines. Dirty paths must be
  exactly artifact plus new test. Current permissions/sandbox remain binding.

### 3. Context

The oracle must distinguish process-registry exclusion from OS descriptor
locking, forbid stale/PID/time cleanup, prove persistent inode and fork/process
exit semantics, and keep Windows production unclaimed. It must fail initially
only because the approved module is absent, then be implementable within one
source file and two GREEN attempts.

### 4. Steps and gates

1. Reconfirm hashes/lines/status and static 48-shape/75-node count.
2. Review every import, name, signature, fixture, helper, parametrization, and
   assertion for syntax/collection blockers after the module exists.
3. Trace every TK2-E002 contract item to an independent oracle. Explicitly
   check both lock layers, all release-before-FD rules, persistent inode,
   symlink/nonregular/no-follow identity, exact error mapping, fork at-fork
   behavior, process handshakes/deadlines/cleanup, Windows seam, and residual.
4. Attempt false-positive implementations: registry-only, flock-only, lock
   entry unlink/recreate, PID/TTL reclaim, wrong-owner unlock, broad errno map,
   monkeypatched platform fallback, installed-copy child import, sleep/timing
   inference, and skipped Windows seam.
5. Confirm current-source prediction is exactly one missing-module collection
   error, not syntax/setup/wrong import. P0=P1=P2=0 is required for ACCEPT.
6. Return exact line anchors and minimum correction for every finding. Do not
   edit, execute, or self-disposition a finding.

### 5. Execution discipline

- Delegation: `spawn-send-wait`; model tier: `deep`; read-only text/hash/status
  only. No pytest/Python/Ruff/formatter or other executable validation.
- Stop on anchor/allowlist drift or any P0/P1/P2. Reviewer must not edit, stage,
  commit, push, install, touch CAD/environment/network/model, or start TK3.

### 6. Delivery boundary

Deliver only the static finding ledger and ACCEPT/REJECT. Controller retains
all artifact/test corrections, RED, production implementation, gates, Git, and
publication authority.

### 7. Final report

Return anchors, shape/node count, missing-module RED prediction, contract
coverage, false-positive analysis, platform residual accuracy, P0/P1/P2,
prohibited-action confirmation, and verdict.

## TK2 Static Test Review Rejection — TK2-E004

Two distinct read-only shards reviewed the exact TK2-E003 candidate without
executing it and returned REJECT. Shard A reported P0/P1/P2 = 0/5/2; shard B
reported 0/8/1. The controller accepted every finding. No source existed and
no RED, Python, pytest, Ruff, formatter, environment, dependency, CAD, network,
model, Git mutation, TK3, or external action occurred.

The blocking gaps were: release-validation tests did not independently prove
that invalid/wrong owner, wrong manager, forged lease, and repeated release
leave the adapter and descriptor untouched; acquisition and release error
cleanup did not fully prove close/deregister/order/reacquisition; generic and
project-write APIs lacked cross-domain contention; entry identity and current
euid were incomplete across path stat, fd stat, and post-lock recheck; POSIX
capabilities and open flags were bundled or weak; Windows flags and release
mapping were incomplete; inherited manager, at-fork registry reset, and child
cleanup/reaping were incomplete; the orphan-grandchild shape could leak; and
the static no-TTL/no-deletion oracle missed ImportFrom, aliases, bare calls,
additional deletion/time calls, and broad exception handlers. The P2 findings
also required exact enum/string proxy rejection, token uniqueness, immutable
public state, and bounded thread/fork cleanup.

## TK2 Corrected Test Candidate — TK2-E005

Starting from artifact
`b7ec1c3f74f746c5d97264bccff39c3a71b7eee1175f3292163d008eb11f5df6`
at 4526 lines, the controller corrected only
`tests/test_workflow_lease.py`. The frozen corrected test hash is
`fd8b6310e40fd6d945101dbf0cc9360db0d0d9aec988dd48919db68348961051`
at 1465 lines. The production source remains absent.

The correction adds independent adapter/close spies for every release
validation boundary; exact forged/subclass/proxy rejection; cross-API
contention; acquisition and release rollback with unlock-close-deregister
order, terminal state, persistent inode, and reacquisition; token uniqueness
and immutable fields; pre-existing future/expired/binary content invariance;
path-stat, fd-fstat, and post-lock identity/current-euid checks; component and
entry open-flag oracles; one-at-a-time missing POSIX primitive checks; Windows
binary/noninheritable/non-destructive flags and operation-aware release errors;
and a direct bounded fork handshake proving inherited lease/manager rejection
plus fresh child-manager reacquisition after parent release. The orphan shape
was removed. The AST oracle now covers Import/ImportFrom aliases, attribute and
bare calls, TTL/time sources, deletion/replacement APIs, and broad handlers.

Static correction inspection found no whitespace error and no allowlist drift.
The corrected candidate still imports the absent source directly, so the only
permitted RED prediction remains one missing-module collection error. The file
is frozen pending a distinct full static acceptance; it must not be executed or
changed before that disposition.

## Task Packet TK2-RP2 — Corrected Static Lease Test Review

### 1. Authorization and anchor

TK-R1/TK-A02 and TK-R2/TK-A03 authorize this bounded read-only correction
review. Repository branch/HEAD/upstream remain
`codex/task-kernel-phase2@1ef1d5df4aaa67e43ea0041a0404cdb9b3388e4a`.
Review exactly the frozen test
`fd8b6310e40fd6d945101dbf0cc9360db0d0d9aec988dd48919db68348961051`
at 1465 lines against TK2-E002 and TK2-E004. Lease source must remain absent.

### 2. Required disposition

Review the entire file, not only changed lines. Recount test shapes and static
parameter expansion; inspect syntax/name/signature/fixture consistency; map
every public, registry, filesystem, adapter, process, cleanup, and residual
contract to an independent behavior oracle; and retry every false-positive
implementation listed in TK2-RP1. In particular confirm exact release-before-FD
validation, persistent inode on all failures, individually missing primitives,
post-lock ordering, fork child registry reset/fresh-manager acquisition, one
waitpid waiter, Windows operation-aware mapping, and AST alias/broad-handler
coverage.

P0=P1=P2=0 is required for ACCEPT. Return exact anchors and minimum corrections
for any finding. Stay read-only: no Python/pytest/Ruff/formatter, edits, source,
stage/commit/push, environment/CAD/network/model action, TK3, or self-disposition.

## TK2 Static Review Convergence — TK2-E006

The TK2-E005 candidate did not pass its distinct static review. At test hash
`fd8b6310e40fd6d945101dbf0cc9360db0d0d9aec988dd48919db68348961051`
and 1465 lines, the reviewer returned REJECT with P0/P1/P2 = 0/7/1. The next
corrected candidate,
`d4e5aa9e9599af71739245df68c6458d8bf09c528a91e5a3505ff789a188801f`
at 1665 lines, received REJECT with P0/P1/P2 = 0/11/6. The subsequent whole-file
candidate,
`d06f41cc291c98622947041daa447923af1802fe2ff105f70896748b0ad406b8`
at 2204 lines and 70 test shapes, was reviewed by two distinct read-only
reviewers. They independently returned REJECT with 0/8/0 and 0/2/6. Every
finding was accepted; no severity was downgraded and no user approval was
requested because all corrections remain inside the approved TK2 outcome.

Across those convergence rounds, the controller strengthened exact public
exports/enums/signatures and fixed non-reflective errors; exact resource text,
Unicode, identifier, and token boundaries; trust and capability failure before
storage work; registry ordering and its platform/root-device/root-inode key;
generic/project cross-contention; root and entry path-versus-FD checks before
and after lock; full metadata, euid, mode, kind, and link-count checks; failure
cleanup, no-unlock acquisition errors, exact release ordering, and both public
release paths; persistent inode/content; fork creator-PID, inherited-FD close,
registry reset, and child payload evidence; complete POSIX capability fakes;
Windows adapter boundaries; and the static no-TTL/no-deletion oracle.

The last independent findings additionally required exact preservation of
spaces and Unicode normalization forms, target metadata/no-directory-entry
damage, public call signatures, terminal repeat after release failure, device
identity injection, complete single-line control representatives, and parent
fork cleanup that reaps the child even when release reports an error. Those
corrections are now included in the next candidate. Throughout E005-E006,
`src/vibecad/workflow/lease.py` remained absent and no Python, pytest, Ruff,
formatter, source implementation, dependency/environment/CAD/network/model
action, Git metadata mutation, TK3 work, or external publication occurred.

## TK2 Final Static Test Candidate — TK2-E007

Starting from artifact
`fd6f0337a7eec2793fc5ed5e23144d8b49354277842e277c525b2dcfb55b47a7`
at 4603 lines, the controller changed only the approved TK2 test and this
append-only artifact. The candidate test hash is
`97389d8375090699bb5967c12b6ee214c976c336d9e5e76e87d999ba6fe9e86a`
at 2606 lines with 80 static test shapes. A distinct reviewer must independently
recount parameter expansion. HEAD and upstream remain
`1ef1d5df4aaa67e43ea0041a0404cdb9b3388e4a`; the only dirty paths are this
artifact and the new test, and the lease source remains absent.

The candidate directly imports the absent approved module, so the only valid
initial RED prediction remains one collection error caused by
`ModuleNotFoundError: vibecad.workflow.lease`. It is frozen pending a complete
static ACCEPT with P0=P1=P2=0. Any finding reopens only static correction and
review; executing RED before that acceptance is prohibited.

## Task Packet TK2-RP5 — Final Whole-File Static Acceptance

### 1. Authorization

TK-R1/TK-A02 and TK-R2/TK-A03 authorize this read-only TK2 review and the
already approved correction outcome. This packet inherits higher-priority
instructions, directory-scoped repository instructions, the exact allowlist,
and the current host permission model and sandbox. The Skill, artifact, and
packet cannot expand authority or bypass those controls. Do not request the
same approval again.

### 2. Workspace anchor

- Repository: `/Users/wangtao/Documents/DevProject/vibecad`.
- Branch/HEAD/upstream:
  `codex/task-kernel-phase2@1ef1d5df4aaa67e43ea0041a0404cdb9b3388e4a`.
- Test:
  `97389d8375090699bb5967c12b6ee214c976c336d9e5e76e87d999ba6fe9e86a`,
  2606 lines and 80 static test shapes.
- Source must be absent. Dirty scope is only the artifact and test. The current
  permission model and sandbox remain binding.

### 3. Context

Review the final test oracle against TK2-E002 plus every accepted finding in
TK2-E004 and TK2-E006. The test must be syntactically and semantically
implementable within the one approved source file, distinguish process and OS
exclusion, preserve every unsafe target, fail closed on uncertain roots or
platforms, and remain deterministic under threads, subprocesses, and fork.

### 4. Steps and gates

1. Reconfirm hash, lines, status, source absence, test-shape count, and static
   parametrized-node expansion without executing Python or the test suite.
2. Inspect every import, helper, fixture, decorator, signature, parameter ID,
   assertion, monkeypatch seam, and cleanup path for collection or false-green
   behavior after the source exists.
3. Map every public, root, registry, entry, adapter, release, process, fork,
   persistent-inode, and platform-residual contract to an independent oracle.
4. Retry adversarial implementations: registry-only, OS-lock-only, path-only
   identity, inode-only identity, owner validation after unlock, normalization,
   target mutation, lock deletion/recreation, PID/TTL reclaim, broad errno
   mapping, inherited-object work before PID validation, incomplete at-fork
   cleanup, unlocked platform fallback, and AST aliases.
5. Confirm the predicted RED is exactly the missing approved module. ACCEPT
   requires P0/P1/P2 = 0/0/0; otherwise return every exact anchor and minimum
   correction without editing or self-disposition.

### 5. Execution discipline

Use `spawn-send-wait`, deep review, `repo-artifact`, and read-only static shell
inspection. Do not run Python, pytest, Ruff, a formatter, CAD, install, access
the network/model, edit, stage, commit, push, or start TK3. Stop on anchor or
allowlist drift.

### 6. Delivery boundary

Deliver only the independent finding ledger, counts, RED prediction, platform
residual assessment, prohibited-action confirmation, and ACCEPT/REJECT. The
controller owns all corrections, RED, implementation, gates, staging, commit,
push, recovery evidence, and transition to TK3.

### 7. Final report

Return exact anchors, static shape/node counts, contract and false-positive
coverage, P0/P1/P2, residual accuracy, confirmation of zero prohibited actions,
and a final verdict.

## TK2 Final Static Candidate Rejection — TK2-E008

Two distinct reviewers completed TK2-RP5 against exact test
`97389d8375090699bb5967c12b6ee214c976c336d9e5e76e87d999ba6fe9e86a`
at 2606 lines and artifact
`951e9c516d9ef18a68599f3ef847a918a62cea5157b859875ec8119276e4b16c`
at 4725 lines. They returned REJECT with P0/P1/P2 = 0/2/1 and 0/2/4.
The union is three distinct P1 findings and four distinct P2 findings; every
finding was accepted without downgrading.

The P1 findings were: `phase` and `identity_field` parametrization decorators
had drifted from their intended entry-metadata tests onto unrelated resource
and project identifier tests, producing latent collection errors once the
source exists; `trust=None` was not included in the before-I/O/adapter zero-call
oracle; and path-like resource tests did not prohibit the raw identifier from
first selecting an external path before the hashed lock entry was used. The P2
findings were: the registry key did not directly prove its root-device field;
the missing-capability matrix omitted `O_CREAT`; close-failure terminal state
did not repeat both public release paths with zero further work; and the exact
Unicode/space acquisition loop entered cleanup only after all acquisitions,
which could leak the first lease under a deliberately incorrect normalizing
implementation.

The controller corrected the decorator placement; added `None` to the strong
trust-order oracle; added raw `open/stat/lstat/chmod` path-selection spies plus
complete sentinel metadata preservation; made the normalization cleanup cover
partial acquisition; asserted the exact `(platform, st_dev, st_ino,
resource_key)` tuple passed to registry removal; added both `O_CREAT` and the
root-open `O_RDONLY` primitive to the one-at-a-time capability matrix; and
proved both release entry points remain terminal and perform no work after a
close failure. These changes remain wholly inside TK2-E002 and the approved
allowlist.

No RED, Python, pytest, Ruff, formatter, production source, dependency,
environment, CAD, network, model, Git metadata mutation, TK3 work, or external
action occurred. HEAD/upstream remained the pushed TK1 anchor and the source
remained absent.

## TK2 Corrected Final Static Candidate — TK2-E009

Starting from the TK2-E007 artifact revision above, the corrected frozen test
is
`df088fd4520eb9702685c72df0bb825e98ece885cc91b14a42adea39db104eea`
at 2717 lines with 80 static test shapes. Moving the two decorators produces
the reviewers' corrected 159-node expansion; adding the two individually
missing capability cases yields an expected static expansion of 161 nodes.
The lease source remains absent. Static shell inspection reports no trailing
whitespace, over-100-character test line, duplicate top-level test name, or
Git diff whitespace error.

This candidate remains frozen. The only valid initial RED prediction is still
one top-level import failure for the absent `vibecad.workflow.lease`; a distinct
whole-file static ACCEPT at P0/P1/P2 = 0/0/0 is required before executing it.

## Task Packet TK2-RP6 — Corrected Whole-File Static Acceptance

### 1. Authorization

TK-R1/TK-A02 and TK-R2/TK-A03 authorize this read-only TK2 review and closure
of all TK2-E004/E006/E008 findings. This packet inherits all higher-priority
instructions, repository instructions, exact allowlist, and the current host
permission model and sandbox. Neither the Skill, artifact, nor packet expands
authority. Do not request duplicate approval.

### 2. Workspace anchor

- Repository: `/Users/wangtao/Documents/DevProject/vibecad`.
- Branch/HEAD/upstream:
  `codex/task-kernel-phase2@1ef1d5df4aaa67e43ea0041a0404cdb9b3388e4a`.
- Test:
  `df088fd4520eb9702685c72df0bb825e98ece885cc91b14a42adea39db104eea`,
  2717 lines, 80 shapes, expected 161 static nodes.
- Source must remain absent; dirty paths must remain only artifact and test.
  The current permission model and sandbox remain binding.

### 3. Context

Perform a fresh whole-file review against TK2-E002 and every rejected-candidate
finding. In addition to the original public, exclusion, filesystem, release,
process, fork, and platform contracts, explicitly verify corrected decorator
ownership, partial-acquisition cleanup, trust-before-I/O for every invalid
form, raw path non-selection, exact registry tuple, capability completeness,
and terminal repeat after both adapter and close errors.

### 4. Steps and gates

1. Reconfirm exact hashes, line count, source absence, status, 80 test shapes,
   and the 161-node static expansion without executing Python or tests.
2. Inspect every helper, fixture, decorator, call signature, monkeypatch seam,
   loop, and cleanup path for collection blockers, unbounded waits, leaks,
   internal inconsistency, and implementation impossibility.
3. Map every TK2-E002 item and all E004/E006/E008 findings to independent
   assertions, then retry registry-only, OS-only, normalization, raw-path,
   target-mutation, omitted-device, wrong release ordering, inherited-FD,
   PID/TTL deletion, broad-error-map, and platform-fallback implementations.
4. Confirm the current source-absent prediction is exactly one missing-module
   collection error and that a conforming source can collect every test.
5. ACCEPT only at P0/P1/P2 = 0/0/0. Otherwise return all exact anchors and
   minimum corrections; do not edit or self-disposition.

### 5. Execution discipline

Use `spawn-send-wait`, deep independent review, `repo-artifact`, and read-only
static shell inspection. Do not run Python, pytest, Ruff, formatter, CAD,
install, access network/model services, edit, stage, commit, push, or start TK3.
Stop on anchor or allowlist drift.

### 6. Delivery boundary

Deliver only the static counts, finding ledger, false-positive analysis,
missing-module RED prediction, platform residual assessment, prohibited-action
confirmation, and verdict. The controller retains correction, RED,
implementation, gates, Git, recovery, and continuation authority.

### 7. Final report

Return exact anchors, 80/161 count confirmation or corrected count, complete
contract/finding coverage, P0/P1/P2, residual accuracy, zero prohibited-action
confirmation, and ACCEPT/REJECT.

## TK2 Corrected Candidate Rejection — TK2-E010

Two independent reviewers completed TK2-RP6 against exact test
`df088fd4520eb9702685c72df0bb825e98ece885cc91b14a42adea39db104eea`
at 2717 lines and artifact
`be1c3a79b14608faf8f77a76c75952db43826dd78aaf8a7bf5057f18da5fc21c`
at 4845 lines. Both confirmed 80 test shapes and 161 static expanded nodes,
and confirmed that every TK2-E008 correction was closed. They returned REJECT
with P0/P1/P2 = 0/1/0 and 0/2/0. The union contains two P1 findings.

First, the raw-path oracle intercepted `os.open/stat/lstat/chmod` but not
`Path.open`, `io.open`, `builtins.open`, aliases captured before monkeypatch,
or equivalent read-only probes such as access, directory enumeration,
readlink, statvfs, and chdir. A deliberately wrong implementation could select
an absolute resource path without changing the sentinel metadata. Second, the
fork proof allowed the child to call inherited or fresh VibeCAD APIs before
the parent closed its descriptor. An implementation could omit at-fork close,
then perform lazy child cleanup on that first API call and falsely pass.

The controller expanded the runtime raw-path oracle to `Path.open`, `io.open`,
`builtins.open`, access, chdir, listdir, readlink, scandir, and statvfs; added a
static rule that permits only a resolved direct `os.open` attribute call,
detects simple captured pathname-call aliases, and forbids the added pathname
probe surface. The fork handshake now blocks the child immediately after fork
before any VibeCAD API. While the child remains blocked, the parent first proves
contention, releases through a close-only adapter, and proves an independent
process can acquire and release the same resource. Only then may the child call
inherited/fresh APIs. This distinguishes at-fork descriptor closure from lazy
cleanup.

No RED, Python, pytest, Ruff, formatter, production source, dependency,
environment, CAD, network, model, Git metadata mutation, TK3 work, or external
action occurred. HEAD/upstream remained the pushed TK1 anchor and the lease
source remained absent.

## TK2 Static Acceptance Candidate — TK2-E011

The new frozen test is
`18673569b30d3d4cbb6cc1fd7066a9bc5c8f3a509b9d4b61d702a5c81102ae40`
at 2808 lines. It retains 80 test shapes and the previously confirmed 161
static expanded nodes; the correction changes only assertions, spies, and
handshake ordering. Starting artifact revision was TK2-E009/RP6 artifact
`be1c3a79b14608faf8f77a76c75952db43826dd78aaf8a7bf5057f18da5fc21c`
at 4845 lines. Static shell checks report no trailing whitespace,
over-100-character test line, duplicate top-level test name, or Git diff
whitespace error. Source remains absent and the only initial RED prediction
remains the missing approved module.

## Task Packet TK2-RP7 — Static Zero-Finding Acceptance

### 1. Authorization

TK-R1/TK-A02 and TK-R2/TK-A03 authorize this final read-only TK2 test review
and closure of all prior findings. This packet inherits higher-priority and
repository instructions, the exact allowlist, and the current host permission
model/sandbox. The Skill, artifact, and packet do not expand authority. Do not
request duplicate approval.

### 2. Workspace anchor

- Repository: `/Users/wangtao/Documents/DevProject/vibecad`.
- Branch/HEAD/upstream:
  `codex/task-kernel-phase2@1ef1d5df4aaa67e43ea0041a0404cdb9b3388e4a`.
- Test:
  `18673569b30d3d4cbb6cc1fd7066a9bc5c8f3a509b9d4b61d702a5c81102ae40`,
  2808 lines, 80 shapes, 161 static nodes.
- Source must remain absent; dirty paths are only artifact and test. Current
  permission/sandbox remains binding.

### 3. Context

Freshly review the complete final oracle against TK2-E002 and every rejection
through TK2-E010. Explicitly challenge non-`os.open` and aliased raw-path
selection, every read-only pathname probe, and lazy fork-child descriptor or
registry cleanup before reviewing the original public, filesystem, exclusion,
release, persistence, process, adapter, and residual contracts.

### 4. Steps and gates

1. Reconfirm hash, lines, source absence, dirty scope, 80 shapes, and 161 nodes
   using static read-only inspection only.
2. Inspect every import, fixture, decorator, signature, spy, AST rule, thread,
   subprocess, fork handshake, deadline, and cleanup path for collection,
   false-green, leak, or implementation blockers.
3. Replay every prior adversary plus non-OS open, captured aliases, read-only
   path probes, child lazy FD cleanup, and child lazy registry reset.
4. Confirm one missing-module RED prediction and accurate unclaimed Windows
   residual. ACCEPT only with P0/P1/P2 = 0/0/0; otherwise report all exact
   findings and minimum corrections without editing or self-disposition.

### 5. Execution discipline

Use `spawn-send-wait`, deep independent review, `repo-artifact`, and read-only
static shell inspection. Do not execute Python, pytest, Ruff, formatter, CAD,
install, network/model access, edit, stage, commit, push, or TK3. Stop on anchor
or allowlist drift.

### 6. Delivery boundary

Deliver only the counts, finding ledger, adversarial analysis, RED prediction,
platform residual assessment, prohibited-action confirmation, and verdict.
The controller retains every state-changing action and continuation decision.

### 7. Final report

Return exact anchors, 80/161 confirmation, full contract and adversary coverage,
P0/P1/P2, residual accuracy, zero prohibited actions, and ACCEPT/REJECT.

## TK2 Static Acceptance Rejection — TK2-E012

The two TK2-RP7 reviewers independently confirmed exact test
`18673569b30d3d4cbb6cc1fd7066a9bc5c8f3a509b9d4b61d702a5c81102ae40`
at 2808 lines, artifact
`0415a5199a3cb7aaad92b585ccefe96149d4e4317a791ba20d81e59d159b2a5c`
at 4953 lines, 80 test shapes, 161 static nodes, source absence, and the
pushed TK1 anchor. They returned REJECT with P0/P1/P2 = 0/2/0 and 0/3/0.
The three-finding union was accepted in full.

The raw-path rule still missed module-import-time captures such as
`_raw_open = open` and `_raw_stat = os.stat`, transitive/default captures,
component-by-component `os.open` below a non-root directory FD, and pathconf or
xattr probes. The fork test proved pre-API FD closure but did not directly
observe that the copied registry and its lock were reset before the first child
API. The no-TTL AST rule did not resolve aliases such as
`_clock = os.times`; broad exception aliases had the analogous supporting gap.

The controller now tracks every returned `os.open` FD to its recursively
resolved parent path and removes that lineage on close; expands runtime/static
path probes through pathconf and xattrs; resolves direct, transitive, annotated,
and default-parameter captures of path, content, deletion, and time calls; bans
non-`os` direct open/stat/lstat calls, captured calls, sensitive dynamic
`getattr`, and the newly identified time attributes. Broad exception aliases,
including tuples, now feed the broad-handler/suppression oracle. Before the
fork child emits its first ready message or calls any VibeCAD API, it directly
asserts the private copied reservation mapping is empty and the registry lock
identity differs from the parent. The production implementation must therefore
expose `_PROCESS_RESERVATIONS` and `_PROCESS_REGISTRY_LOCK` as the exact private
state used by its at-fork callback.

No RED, Python, pytest, Ruff, formatter, production source, dependency,
environment, CAD, network, model, Git metadata mutation, TK3 work, or external
action occurred. The lease source remained absent.

## TK2 Zero-Finding Static Candidate — TK2-E013

The frozen test is
`e7665b7c7f0116ed15a39ffec7f40013d623bc548a9062fbbff106a8ff6361f3`
at 2930 lines. It retains 80 test shapes and 161 static expanded nodes. Static
shell checks report no trailing whitespace, over-100-character test line,
duplicate top-level test name, or Git diff whitespace error. The only initial
RED prediction remains the absent `vibecad.workflow.lease` import.

## Task Packet TK2-RP8 — Zero-Finding Static Acceptance

### 1. Authorization

TK-R1/TK-A02 and TK-R2/TK-A03 authorize this read-only final test review and
closure of every TK2 finding through E012. Higher-priority instructions,
repository rules, the exact allowlist, and current permission model/sandbox
remain binding; no artifact or packet expands authority. Do not request
duplicate approval.

### 2. Workspace anchor

- Repository: `/Users/wangtao/Documents/DevProject/vibecad`.
- Branch/HEAD/upstream:
  `codex/task-kernel-phase2@1ef1d5df4aaa67e43ea0041a0404cdb9b3388e4a`.
- Test:
  `e7665b7c7f0116ed15a39ffec7f40013d623bc548a9062fbbff106a8ff6361f3`,
  2930 lines, 80 shapes, 161 static nodes.
- Source must be absent; dirty paths remain only artifact and test. Current
  permission model/sandbox remains binding.

### 3. Context

Perform another complete static acceptance against TK2-E002 and every
subsequent accepted finding. Focus first on import-time/transitive/default path
aliases, recursive directory-FD lineage, all direct pathname probes, time and
broad-exception aliases, and direct pre-API child registry/lock reset; then
repeat the entire public/filesystem/exclusion/release/process/platform review.

### 4. Steps and gates

1. Reconfirm exact anchor, source absence, dirty scope, lines, 80 shapes, and
   161 nodes by static read-only inspection only.
2. Inspect every import, fixture, decorator, helper, monkeypatch, AST traversal,
   bounded fixed-point loop, subprocess/fork handshake, and cleanup path for
   collection, false-green, leak, hang, or implementation impossibility.
3. Replay all prior adversaries, including captured `open/stat/lstat`, chained
   directory opens, dynamic path/time lookup, tuple exception aliases, lazy FD
   close, lazy registry clear, and inherited lock reuse.
4. Confirm one missing-module RED prediction and accurate Windows residual.
   ACCEPT requires P0/P1/P2 = 0/0/0; otherwise return every finding and minimum
   correction without editing or self-disposition.

### 5. Execution discipline

Use `spawn-send-wait`, deep independent review, `repo-artifact`, and read-only
static shell inspection. Do not execute Python, pytest, Ruff, formatter, CAD,
install, network/model access, edit, stage, commit, push, or TK3. Stop on anchor
or allowlist drift.

### 6. Delivery boundary

Deliver only counts, findings, adversarial analysis, RED prediction, residual
assessment, prohibited-action confirmation, and verdict. The controller owns
every state-changing action and continuation decision.

### 7. Final report

Return exact anchors, 80/161 confirmation, full contract/adversary coverage,
P0/P1/P2, residual accuracy, zero prohibited actions, and ACCEPT/REJECT.

## TK2 Zero-Finding Candidate Rejection — TK2-E014

Two independent TK2-RP8 reviewers reconfirmed exact test
`e7665b7c7f0116ed15a39ffec7f40013d623bc548a9062fbbff106a8ff6361f3`
at 2930 lines, artifact
`ceb31c8b2a4c392cf433ff40e124e4607bcb90fd3a9f9b237f11b54e7fff4cbf`
at 5058 lines, 80 static test shapes, 161 static expanded nodes, source
absence, and the pushed TK1 anchor. They returned REJECT with P0/P1/P2 =
0/3/0 and 0/4/0. The full union is retained without downgrade.

The runtime pathname oracle still lost the lineage of descriptors retained
before spy installation or copied through `dup`/`dup2`; it also omitted
`setxattr` and `removexattr`, allowing reversible external-path mutation. The
AST oracle still allowed imported or computed `getattr`, callable wrappers
such as `partial`, container/subscript capture, and broad-exception default
aliases. Finally, the fork first-frame names were not behavior-bound strongly
enough to exclude an implementation that coordinated through hidden registry
and lock objects while exposing decoy `_PROCESS_RESERVATIONS` and
`_PROCESS_REGISTRY_LOCK` globals.

The controller moved manager construction under the complete runtime spy,
tracks `dup`/`dup2` lineage, rejects every unresolved path operation, and
intercepts both xattr mutation calls without executing them. The exported
registry is now a guarded mapping whose mutation fails unless the exact
exported lock is held; normal reserve, contention, drop, fork preparation,
parent preservation, and child replacement are all tied to those same object
identities and the exact registry tuple. The AST oracle now resolves imported
and assigned module/string aliases, folds constant string concatenation,
rejects unknown or sensitive dynamic lookup, rejects indirect call targets,
and permits path-sensitive callables only as immediate direct
`os.open/stat/lstat(...)` callees. Sensitive callable references and broad
exception references/defaults are independently rejected.

No RED, Python, pytest, Ruff, formatter, production source, dependency,
environment, CAD, network/model access, Git metadata mutation, TK3 work, or
external action occurred. The source remains absent.

## TK2 RP9 Static Acceptance Candidate — TK2-E015

The corrected frozen test is
`3ea574abe56ed5bc0a5e5121fa74489a149a7d49822e9e943eef715533459305`
at 3226 lines. It retains 80 static test shapes and the previously confirmed
161 static expanded nodes; only helpers and assertions changed. Static shell
checks report no trailing whitespace, over-100-character test line, duplicate
top-level test name, or Git diff whitespace error. The approved production
source remains absent. The sole valid initial RED prediction remains one
top-level import failure for `vibecad.workflow.lease`.

## Task Packet TK2-RP9 — Static Zero-Finding Acceptance

### 1. Authorization

TK-R1/TK-A02 and TK-R2/TK-A03 authorize this read-only TK2 review and closure
of all findings through TK2-E014. Higher-priority instructions, repository
rules, the exact allowlist, and the current permission model/sandbox remain
binding; neither this artifact nor packet expands authority. Do not request
duplicate approval.

### 2. Workspace anchor

- Repository: `/Users/wangtao/Documents/DevProject/vibecad`.
- Branch/HEAD/upstream:
  `codex/task-kernel-phase2@1ef1d5df4aaa67e43ea0041a0404cdb9b3388e4a`.
- Test:
  `3ea574abe56ed5bc0a5e5121fa74489a149a7d49822e9e943eef715533459305`,
  3226 lines, 80 shapes, 161 expected static nodes.
- Source must remain absent; dirty paths remain only artifact and test.
  Current permission and sandbox constraints remain binding.

### 3. Context

Perform a fresh whole-file static review against TK2-E002 and every accepted
finding through E014. Begin with the new guarded registry/lock helpers,
pre-spy and copied/unknown descriptor lineage, mutation probes, structural
sensitive-callable rule, dynamic lookup, indirect calls, and broad-exception
references. Then repeat the complete public, filesystem, exclusion, release,
process, fork, platform, and residual review.

### 4. Steps and gates

1. Reconfirm exact anchor, test hash and lines, source absence, dirty scope,
   80 shapes, and 161 expected nodes using static read-only inspection only.
2. Inspect every import, helper, fixture, decorator, signature, spy, guarded
   mapping mutation route, AST traversal/fixed point, thread, subprocess, fork
   handshake, deadline, and cleanup path for collection blockers,
   false-positives, false-greens, leaks, hangs, or implementation impossibility.
3. Replay every prior adversary plus hidden/decoy registries, mutation outside
   the exported lock, import-time root FDs, duplicated descriptors, xattr
   mutation, imported/computed `getattr`, partial/container capture, indirect
   calls, and default-parameter broad exception aliases.
4. Confirm the absent-source prediction is exactly one missing-module import
   failure and that the Windows live residual remains explicitly unclaimed.
   ACCEPT requires P0/P1/P2 = 0/0/0; otherwise report every finding and minimum
   correction without editing or self-disposition.

### 5. Execution discipline

Use `spawn-send-wait`, deep independent review, `repo-artifact`, and read-only
static shell inspection. Do not execute Python, pytest, Ruff, a formatter,
CAD, install, network/model access, edit, stage, commit, push, or begin TK3.
Stop on anchor or allowlist drift.

### 6. Delivery boundary

Deliver only static counts, finding ledger, adversarial analysis, RED
prediction, platform residual assessment, prohibited-action confirmation, and
ACCEPT/REJECT. The controller retains correction, RED, implementation, gates,
Git, recovery evidence, and continuation authority.

### 7. Final report

Return exact anchors, 80/161 confirmation or correction, complete contract and
adversary coverage, P0/P1/P2, residual accuracy, confirmation of zero
prohibited actions, and the final verdict.

## TK2 RP9 Static Candidate Rejection — TK2-E016

Two independent reviewers completed TK2-RP9 against exact test
`3ea574abe56ed5bc0a5e5121fa74489a149a7d49822e9e943eef715533459305`
at 3226 lines and artifact
`688db81fad1d3072fca4bdb3198ea301d92080af959e1d20decde01f76bb6fe3`
at 5173 lines. They reconfirmed 80 test shapes, 161 static expanded nodes,
source absence, the pushed TK1 anchor, the single missing-module RED
prediction, and the unclaimed Windows live residual. Their dispositions were
REJECT at P0/P1/P2 = 0/5/0 and 0/3/2. The complete union is retained.

The dict-subclass registry probe allowed base-dict bypass and did not enforce
atomic absent-check plus insertion under the exact exported lock. The fork
test did not prove the parent callback released that lock, and runtime mirroring
still left room for an alternate hidden authority. Dynamic/static reflection
through `__builtins__`, `__import__`, `inspect.getattr_static`, or unresolved
exception aliases remained fail-open. Conversely, the structural callable
rule rejected the exact `open`/`stat` support-set identity comparisons required
by the capability contract, while cardinality/name-only checks could pass.
Trust and capability gates omitted several read-only storage probes. Pure
import did not statically exclude module-level filesystem/network side
effects. Entry flags omitted `O_CREAT`, root flags did not prove read-only
access mode, and two cleanup waits extended beyond their one five-second
deadline.

The controller replaced the registry probe with a non-dict `MutableMapping`:
all reads and writes require the exact exported probe lock, and an absent read
must be consumed by insertion before the outer lock can release. Test snapshots
acquire the lock without using production mapping operations. Fork now proves
the parent thread and a distinct thread can acquire the same exported lock
immediately after the parent callback. Static authority rules allow mutable
module/process state and lock construction only at the two exported names,
require exact reserve/attach/drop/fork helper references, prohibit alternate
global mutable authority, and bind the registered at-fork callbacks by name.

Reflection/import aliases, unknown exception handler expressions, indirect
lookup helpers, mutable defaults, and shared mutable attribute construction are
now fail-closed. Sensitive `open`/`stat` references receive one structural
exception: exact same-receiver membership comparison inside
`_require_posix_capabilities`. Runtime instrumentation adds its wrappers to
the advertised support sets, and same-name decoy callables must still fail the
fake capability profile. Trust/capability zero-I/O spies now cover OS, Path,
built-in/io open, and socket surfaces. Module-import calls have an exact
allowlist for regex compilation, the one exported RLock, and exact at-fork
registration; forbidden import roots and alternate creation primitives are
closed. Root and entry flag assertions now include read-only access mode and
`O_CREAT`. Reap paths reserve cleanup time but never exceed the original
deadline.

No RED, Python, pytest, Ruff, formatter, production source, dependency,
environment, CAD, network/model access, Git metadata mutation, TK3 work, or
external action occurred. The source remains absent.

## TK2 RP10 Static Acceptance Candidate — TK2-E017

The corrected frozen test is
`4d54ca51071c287784c8179683d84c0649f882250290daa1e7830ed138d6824e`
at 3644 lines. It retains 80 static test shapes and 161 expected static
expanded nodes. Static shell checks report no trailing whitespace,
over-100-character test line, duplicate top-level test name, extra one-second
wait, or Git diff whitespace error. The approved production source remains
absent, and the sole valid initial RED prediction remains one top-level import
failure for `vibecad.workflow.lease`.

## Task Packet TK2-RP10 — Static Zero-Finding Acceptance

### 1. Authorization

TK-R1/TK-A02 and TK-R2/TK-A03 authorize this read-only TK2 review and closure
of all findings through TK2-E016. Higher-priority instructions, repository
rules, the exact allowlist, and current permission model/sandbox remain
binding. Neither artifact nor packet expands authority; do not request
duplicate approval.

### 2. Workspace anchor

- Repository: `/Users/wangtao/Documents/DevProject/vibecad`.
- Branch/HEAD/upstream:
  `codex/task-kernel-phase2@1ef1d5df4aaa67e43ea0041a0404cdb9b3388e4a`.
- Test:
  `4d54ca51071c287784c8179683d84c0649f882250290daa1e7830ed138d6824e`,
  3644 lines, 80 shapes, 161 expected static nodes.
- Source must remain absent; dirty paths remain only artifact and test.
  Current permission and sandbox constraints remain binding.

### 3. Context

Perform a fresh whole-file static review against TK2-E002 and every accepted
finding through E016. Begin with the new non-dict guarded mapping and pending
insert invariant, exported-authority structural rules, parent/child fork-lock
behavior, capability identity exception and decoy cases, gate spies,
import-time side effects, creation flags, and deadline accounting. Then repeat
the complete public, filesystem, exclusion, release, process, platform, and
residual review.

### 4. Steps and gates

1. Reconfirm exact anchor, hash/lines, source absence, dirty scope, 80 shapes,
   and 161 expected nodes through static read-only inspection only.
2. Inspect every import, helper, fixture, decorator, guarded mapping method,
   signature, monkeypatch, AST traversal/fixed point, thread, subprocess, fork
   handshake, deadline, and cleanup route for collection blockers,
   false-positives, false-greens, leaks, hangs, or implementation impossibility.
3. Replay all prior adversaries plus base-dict bypass, unlocked absent-check,
   hidden registry/lock authority, unreleased parent fork lock, builtins/static
   reflection, unresolved exception aliases, capability decoys, pre-gate Path
   enumeration, module-level I/O, alternate file creation, missing create/read
   flags, and cleanup deadline extension.
4. Confirm the sole absent-source RED and accurate Windows residual. ACCEPT
   requires P0/P1/P2 = 0/0/0; otherwise report every finding and minimum
   correction without editing or self-disposition.

### 5. Execution discipline

Use `spawn-send-wait`, deep independent review, `repo-artifact`, and read-only
static shell inspection. Do not execute Python, pytest, Ruff, formatter, CAD,
install, network/model access, edit, stage, commit, push, or begin TK3. Stop on
anchor or allowlist drift.

### 6. Delivery boundary

Deliver only counts, finding ledger, adversarial analysis, RED prediction,
platform residual assessment, prohibited-action confirmation, and verdict.
The controller retains corrections, RED, implementation, gates, Git, recovery,
and continuation authority.

### 7. Final report

Return exact anchors, 80/161 confirmation or correction, complete contract and
adversary coverage, P0/P1/P2, residual accuracy, zero prohibited actions, and
the final disposition.

## TK2 RP10 Static Candidate Rejection — TK2-E018

Two independent reviewers completed TK2-RP10 against frozen test
`4d54ca51071c287784c8179683d84c0649f882250290daa1e7830ed138d6824e`
at 3644 lines and artifact
`a1e8781fd72360ea2190913c57c0730bc58382dce5c045444c9032b9a047a089`
at 5305 lines. They reconfirmed 80 test shapes, 161 expected expanded nodes,
source absence, the pushed TK1 anchor, the single missing-module RED
prediction, and the unclaimed Windows live residual. Their dispositions were
REJECT at P0/P1/P2 = 0/8/1 and 0/4/1; the complete union is retained here.

The guarded registry still allowed blind insertion and enumeration-based
absence checks, while its simultaneous-start test did not alone force an
atomic first-winner decision. Exported registry and lock objects could be
captured through module aliases, defaults, containers, dead references, or
indirect helpers. Reflection through function globals or frames and rebinding
a broad exception under a permitted narrow name remained possible. Import
rules were denylist-based, alternate file creators and `umask` remained, and
explicit-call scanning missed decorators, class construction, metaclasses,
descriptors, and import side effects.

Capability checks did not require callable primitives, valid support-set
types, or exact integer flag types; pre-gate storage primitives could still be
captured before runtime spies were installed. The open-flags oracle classified
directory operations from the very flag being validated, treated bytes paths
incorrectly, did not require exact entry access mode or exact hashed filename,
and did not prove that the pinned-root `os.open` was the first creator. Finally,
an implementation could clear `O_CLOEXEC` after open by making the active file
descriptor inheritable.

The controller made insertion consume an exact keyed miss, added explicit
guarded membership, disabled ordinary registry enumeration, and added a
two-thread first-acquisition oracle. Static authority analysis now rejects
capture of either exported object and verifies exact lock, mapping, keyed
load/store/delete, attach, fork callback, child reset, and manager-release
relationships. Reflection/frame access and allowed-exception rebinding are
closed. Imports, top-level statements, decorators, class bases/bodies, nested
functions, and lambdas now use exact structural rules; alternate creators,
`umask`, and FD-flag mutation routes are rejected.

Capability adversaries now cover seven non-callable primitives, invalid
support containers, and seven non-integer flags, with a narrow structural
exception for direct capability `callable` checks. The complete storage probe
surface participates in alias analysis. Runtime open instrumentation classifies
the returned FD with `fstat`, decodes bytes safely, requires the exact hashed
entry and pinned parent, proves the entry was absent before its sole creator,
checks exact access modes, observes active-FD inheritance, and rejects attempts
to make a descriptor inheritable.

No RED, Python, pytest, Ruff, formatter, production source, dependency,
environment, CAD, network/model access, Git metadata mutation, TK3 work, or
external action occurred. The source remains absent.

## TK2 RP11 Static Acceptance Candidate — TK2-E019

The corrected frozen test is
`408d85aaa5027f541e335f467b31ee8d81ed4a7bc2e96f0cfe224ca7bde3b203`
at 4352 lines. It contains 81 static test shapes and 178 expected expanded
nodes. Static shell checks report no trailing whitespace, over-100-character
test line, duplicate top-level test name, extra one-second wait, or Git diff
whitespace error. The approved production source remains absent, and the sole
valid initial RED prediction remains one top-level import failure for
`vibecad.workflow.lease`.

## Task Packet TK2-RP11 — Static Zero-Finding Acceptance

### 1. Authorization

TK-R1/TK-A02 and TK-R2/TK-A03 authorize this read-only TK2 review and closure
of all findings through TK2-E018. Higher-priority instructions, repository
rules, the exact allowlist, and current permission model remain binding. This
packet does not expand authority; do not request duplicate approval.

### 2. Workspace anchor

- Repository: `/Users/wangtao/Documents/DevProject/vibecad`.
- Branch/HEAD/upstream:
  `codex/task-kernel-phase2@1ef1d5df4aaa67e43ea0041a0404cdb9b3388e4a`.
- Test:
  `408d85aaa5027f541e335f467b31ee8d81ed4a7bc2e96f0cfe224ca7bde3b203`,
  4352 lines, 81 shapes, 178 expected expanded nodes.
- Source must remain absent; dirty paths remain only artifact and test.

### 3. Context

Perform a fresh whole-file static review against TK2-E002 and every accepted
finding through E018. Begin with strict missing-key consumption, enumeration
gating, concurrent first acquisition, authority-use structure, reflection and
exception binding, exact import/top-level/class grammar, capability types,
storage alias capture, FD-derived open classification, exact creator identity,
bytes paths, and final descriptor inheritance. Then replay the complete public,
filesystem, exclusion, release, process, fork, platform, and residual review.

### 4. Steps and gates

1. Reconfirm the exact anchor, hash/lines, source absence, dirty scope,
   81 shapes, and 178 expected nodes through static read-only inspection only.
2. Inspect every import, helper, fixture, decorator, guarded mapping route,
   monkeypatch, AST traversal, thread, subprocess, fork handshake, deadline,
   and cleanup route for collection blockers, false positives, false greens,
   leaks, hangs, or implementation impossibility.
3. Replay all prior adversaries plus blind insert, enumeration absence checks,
   authority capture/defaults/dead refs, function-global/frame reflection,
   exception rebinding, alternate creators, implicit import-time calls,
   malformed capabilities, pre-captured gate primitives, bytes paths, decoy
   regular opens, omitted access flags, and post-open inheritance mutation.
4. Confirm the sole absent-source RED and accurate Windows residual. ACCEPT
   requires P0/P1/P2 = 0/0/0; otherwise report every finding and minimum
   correction without editing or self-disposition.

### 5. Execution discipline

Use deep independent review, repository artifacts, and read-only static shell
inspection. Do not execute Python, pytest, Ruff, formatter, CAD, install,
network/model access, edit, stage, commit, push, or begin TK3. Stop on anchor or
allowlist drift.

### 6. Delivery boundary

Deliver only counts, finding ledger, adversarial analysis, RED prediction,
platform residual assessment, prohibited-action confirmation, and verdict.
The controller retains corrections, RED, implementation, gates, Git,
recovery, and continuation authority.

### 7. Final report

Return exact anchors, 81/178 confirmation or correction, complete contract and
adversary coverage, P0/P1/P2, residual accuracy, zero prohibited actions, and
the final disposition.

## TK2 RP11 Static Candidate Rejection — TK2-E020

Two independent reviewers completed TK2-RP11 against test
`408d85aaa5027f541e335f467b31ee8d81ed4a7bc2e96f0cfe224ca7bde3b203`
at 4352 lines and artifact
`5f4373fb434eb0d033a8363a5933bf81bcd00f1dc451356da55bac1684a09fe9`
at 5435 lines. Both reconfirmed 81 shapes, 178 expected nodes, source
absence, the pushed TK1 anchor, the single missing-module RED prediction, and
the Windows live residual. One reviewer accepted at 0/0/0; the full reviewer
rejected at P0/P1/P2 = 0/4/1. The union is retained.

The still-mutable error-message dictionary could double as hidden process
authority. Import restrictions did not yet restrict the complete `os`
attribute surface, leaving process-launch and alternate external-work calls.
Malformed capability tests used ordinary objects but did not prove rejection
of `bool`, integer subclasses, or set subclasses. A direct-lock requirement on
`ResourceLeaseManager.release` was simultaneously a false red for delegated
cleanup and satisfiable by dead code. Finally, `dup2(..., inheritable=True)`
could create an unobserved inheritable descriptor copy.

The controller removed every mutable module container except the exact exported
process reservation mapping; fixed error messages must therefore use immutable
structure or a pure function. Direct `os` attributes now have an exact
allowlist, and `dup`/`dup2` are forbidden. A new runtime capability-type oracle
uses `True`, an integer subclass, and a set subclass, requiring exact integer
flags and exact support sets. The manager direct-lock shape requirement was
removed; existing runtime release tests continue to prove terminal transition,
unlock, close, and the exact locked drop ordering.

No RED, Python, pytest, Ruff, formatter, production source, dependency,
environment, CAD, network/model access, Git metadata mutation, TK3 work, or
external action occurred. The source remains absent.

## TK2 RP12 Static Acceptance Candidate — TK2-E021

The corrected frozen test is
`2eb7233624e6966013b3e569f189db8c9484763242f9b06862cb2b0b00abc6b2`
at 4394 lines. It contains 82 static test shapes and 179 expected expanded
nodes. Static shell checks report no trailing whitespace, over-100-character
test line, duplicate top-level test name, extra one-second wait, or Git diff
whitespace error. Production source remains absent; the valid initial RED
prediction remains one top-level missing-module import failure.

## Task Packet TK2-RP12 — Static Zero-Finding Acceptance

### 1. Authorization

TK-R1/TK-A02 and TK-R2/TK-A03 authorize this read-only TK2 review and closure
through TK2-E020. Existing rules and the exact allowlist remain binding; do not
request duplicate approval.

### 2. Workspace anchor

- Repository: `/Users/wangtao/Documents/DevProject/vibecad`.
- Branch/HEAD/upstream:
  `codex/task-kernel-phase2@1ef1d5df4aaa67e43ea0041a0404cdb9b3388e4a`.
- Test:
  `2eb7233624e6966013b3e569f189db8c9484763242f9b06862cb2b0b00abc6b2`,
  4394 lines, 82 shapes, 179 expected nodes.
- Source remains absent; dirty paths remain artifact and test only.

### 3. Review focus and gates

Reconfirm the anchor and replay the complete TK2 contract and every finding
through E020. Begin with unique mutable authority, exact `os` attributes,
process-launch denial, exact capability flag/support types, delegated release
without a source-shape false red, and prohibition of descriptor duplication.
Then repeat public API, root/entry, exclusion, cleanup, fork, platform,
deadline, reflection, import-time, and residual analysis. ACCEPT requires
P0/P1/P2 = 0/0/0; otherwise report every finding and minimum correction.

### 4. Execution discipline and delivery

Use only read-only static inspection. Do not execute Python, pytest, Ruff,
formatter, CAD, install, network/model access, edits, Git mutation, or TK3.
Return exact anchors, 82/179 confirmation, complete findings, RED prediction,
Windows residual, prohibited-action confirmation, and ACCEPT/REJECT.

## TK2 RP15 Static Acceptance — TK2-E028

Two independent whole-file static reviewers accepted frozen test
`aaf9eee3a58c49e66229c45b35d9540bc98bd4d23d4d6ee426e4d46d2a923dc9`
at 4679 lines and artifact
`6dd72c6b0eb5d511ed54e266327c5ad04ba6699c129432500cd6ba0cd9512f7d`
at 5719 lines. Both returned P0/P1/P2 = 0/0/0 and reconfirmed 82 unique
test shapes, 179 expected expanded nodes, source absence, exact dirty scope,
the pushed TK1 anchor, the one missing-module RED prediction, implementation
feasibility, and the explicitly unclaimed Windows live residual.

The RP15 test is now frozen through focused RED and production implementation.
The authorized and predicted RED command is exactly:

`PYTHONPATH=src .venv/bin/pytest -q tests/test_workflow_lease.py`

Genuine RED requires exit status 2 with one collection error caused by the
top-level import of absent module `vibecad.workflow.lease`, and no other error.
The command may run exactly once before production source creation. Any other
result is anchor drift and requires recovery before implementation.

No Python, pytest, Ruff, formatter, production source, dependency, environment,
CAD, network/model access, Git mutation, TK3 work, or external action occurred
before this acceptance record.

## TK2 Genuine Focused RED — TK2-E029

The exact authorized command ran once against frozen test
`aaf9eee3a58c49e66229c45b35d9540bc98bd4d23d4d6ee426e4d46d2a923dc9`:

`PYTHONPATH=src .venv/bin/pytest -q tests/test_workflow_lease.py`

It exited 2 after 1.34 seconds with exactly one collection error at test line
28: `ModuleNotFoundError: No module named 'vibecad.workflow.lease'`. Pytest
reported one error in 0.62 seconds and no other collection or execution error.
This exactly matches the frozen prediction and authorizes the first focused
GREEN implementation attempt. Production source remained absent throughout
RED; the test remains frozen.

## TK2 RP14 Static Candidate Rejection — TK2-E026

Two independent reviewers completed TK2-RP14 against test
`1041690b8ff703f9dfec047923298afcd227139ec15e2cd2fa02af2bf4035c1f`
at 4548 lines and artifact
`34ebad224f70632d13f7991f0c32f4734f695fa6b788e85b4dd089e52abb54ce`
at 5648 lines. They reconfirmed 82 shapes, 179 expected nodes, source
absence, the pushed TK1 anchor, the sole missing-module RED, and the Windows
residual. Each rejected at P0/P1/P2 = 0/1/0; their two distinct findings are
retained.

Module-shaped adapter parameters were recognized by guessed variable names,
but private constructor signatures were not fixed. Renaming a parameter and
capturing it on the adapter could bypass exact module attributes. Separately,
the two POSIX capability support sets could be captured outside their intended
checks and reused as a second mutable process authority.

The controller now requires exact `_PosixFileLock(fcntl_module)` and
`_WindowsFileLock(msvcrt_module)` constructor AST signatures. The named module
parameters may only provide exact approved attributes and cannot be captured as
whole objects. Support-set references are sensitive: they are allowed only in
the exact same-receiver membership comparisons and exact built-in-set type
comparisons inside `_require_posix_capabilities`. Capturing, forwarding,
returning, or mutating them is rejected; general set/dictionary mutation methods
are independently forbidden. Type objects may only be queried in direct
comparisons, and additional hierarchy reflection is closed.

No RED, Python, pytest, Ruff, formatter, production source, dependency,
environment, CAD, network/model access, Git mutation, TK3 work, or external
action occurred. The source remains absent.

## TK2 RP15 Static Acceptance Candidate — TK2-E027

The corrected frozen test is
`aaf9eee3a58c49e66229c45b35d9540bc98bd4d23d4d6ee426e4d46d2a923dc9`
at 4679 lines. It retains 82 static test shapes and 179 expected expanded
nodes. Static shell checks report no trailing whitespace, over-100-character
test line, duplicate top-level test name, extra one-second wait, or Git diff
whitespace error. Production source remains absent; the sole valid RED remains
the top-level missing-module import failure.

## Task Packet TK2-RP15 — Static Zero-Finding Acceptance

### 1. Anchor and authority

TK-R1/TK-A02 and TK-R2/TK-A03 authorize this read-only review. Repository:
`/Users/wangtao/Documents/DevProject/vibecad`; branch/HEAD/upstream:
`codex/task-kernel-phase2@1ef1d5df4aaa67e43ea0041a0404cdb9b3388e4a`.
Test:
`aaf9eee3a58c49e66229c45b35d9540bc98bd4d23d4d6ee426e4d46d2a923dc9`,
4679 lines, 82 shapes, 179 nodes. Source remains absent; dirty scope remains
artifact plus test only. Existing rules remain binding.

### 2. Review focus and gate

Reconfirm the anchor and replay the complete contract and all findings through
E026. Begin with adapter formal signatures, module-parameter capture and
attributes, support-set membership/type-only roles, capture and mutation,
ephemeral exact-set typing, type-object containment, and implementability.
Then repeat public API, capabilities, root/entry, unique authority, exclusion,
release, fork inheritance, process boundaries, deadlines, platform, and
residual analysis. ACCEPT requires P0/P1/P2 = 0/0/0; otherwise report every
finding and minimum correction.

### 3. Discipline and delivery

Use only read-only static inspection. Do not execute Python, pytest, Ruff,
formatter, CAD, install, network/model access, edits, Git mutation, or TK3.
Return exact anchors, 82/179 confirmation, complete findings, RED prediction,
Windows residual, prohibited-action confirmation, and ACCEPT/REJECT.

## TK2 RP13 Static Candidate Rejection — TK2-E024

Two independent reviewers completed TK2-RP13 against test
`ebd2f531711e4ad1fd246e6daba60ae263c2208550ecbb89aeb25f4a2fe46b94`
at 4488 lines and artifact
`3565e30862f2c140cdf70c85c73a4a264b7e7d71280100bba4f7572d0f13d6ee`
at 5580 lines. They reconfirmed 82 shapes, 179 expected nodes, source
absence, the pushed TK1 anchor, the sole missing-module RED, and the Windows
residual. Their dispositions were REJECT at P0/P1/P2 = 0/2/0 and 0/1/0.

Imported class objects were omitted from bare-object restrictions and could be
forwarded through containers before invoking unapproved class methods or
reflection. Separately, instance attributes such as Path parser/flavour
surfaces could return transitive modules without a statically qualified module
receiver. The receiver-independent process family also omitted fork and
external-process control leaves.

The controller now subjects imported classes to the same bare-load rule as
modules. Only exact Path construction, exact StrEnum inheritance, and true
annotation positions are allowed; containers, returns, and other forwarding
are rejected. Parser/flavour, environment state, class hierarchy/reflection,
cwd/home, fork/forkpty, kill, wait, environment mutation, and related process
leaves are independently forbidden regardless of receiver qualification.

No RED, Python, pytest, Ruff, formatter, production source, dependency,
environment, CAD, network/model access, Git mutation, TK3 work, or external
action occurred. The source remains absent.

## TK2 RP14 Static Acceptance Candidate — TK2-E025

The corrected frozen test is
`1041690b8ff703f9dfec047923298afcd227139ec15e2cd2fa02af2bf4035c1f`
at 4548 lines. It retains 82 static test shapes and 179 expected expanded
nodes. Static shell checks report no trailing whitespace, over-100-character
test line, duplicate top-level test name, extra one-second wait, or Git diff
whitespace error. Production source remains absent; the sole valid RED
prediction remains the top-level missing-module import failure.

## Task Packet TK2-RP14 — Static Zero-Finding Acceptance

### 1. Anchor and authority

TK-R1/TK-A02 and TK-R2/TK-A03 authorize this read-only review. Repository:
`/Users/wangtao/Documents/DevProject/vibecad`; branch/HEAD/upstream:
`codex/task-kernel-phase2@1ef1d5df4aaa67e43ea0041a0404cdb9b3388e4a`.
Test:
`1041690b8ff703f9dfec047923298afcd227139ec15e2cd2fa02af2bf4035c1f`,
4548 lines, 82 shapes, 179 expected nodes. Source remains absent; dirty scope
remains artifact plus test only. Existing rules remain binding.

### 2. Review focus and gate

Reconfirm the anchor and replay the complete contract and all findings through
E024. Begin with imported-class containers/returns/call arguments, annotation
and constructor exceptions, instance parser/flavour module returns, environment
authority, hierarchy reflection, fork and external-process control leaves, and
module-rule implementability. Then repeat public API, capabilities, root/entry,
unique authority, exclusion, release, fork inheritance, deadlines, platform,
and residual analysis. ACCEPT requires P0/P1/P2 = 0/0/0; otherwise report every
finding and minimum correction.

### 3. Discipline and delivery

Use only read-only static inspection. Do not execute Python, pytest, Ruff,
formatter, CAD, install, network/model access, edits, Git mutation, or TK3.
Return exact anchors, 82/179 confirmation, complete findings, RED prediction,
Windows residual, prohibited-action confirmation, and ACCEPT/REJECT.

## TK2 RP12 Static Candidate Rejection — TK2-E022

Two independent reviewers completed TK2-RP12 against test
`2eb7233624e6966013b3e569f189db8c9484763242f9b06862cb2b0b00abc6b2`
at 4394 lines and artifact
`122dd9d4fbccec7ac47cf64858c5fd819c6022c8a0134870d808e5d1c1575ec9`
at 5513 lines. Both reconfirmed 82 shapes, 179 expected nodes, source absence,
the pushed TK1 anchor, the sole missing-module RED, and the Windows residual.
One accepted at 0/0/0; the full reviewer rejected at P0/P1/P2 = 0/1/0.

Exact `os` attributes did not close transitive module access. An implementation
could reach process functions through `threading._os` or store hidden authority
in imported mutable state such as `re._cache`, including after first capturing
those objects. This bypassed both the direct-`os` rule and the source-owned
mutable-container rule.

The controller added independent bans for every process-launch call family and
replaced the single-module check with exact attribute tables for every allowed
module, imported class, and module-shaped adapter parameter. Bare module objects
may now appear only as the receiver of an approved attribute; the sole argument
exceptions are the exact `os` capability check and exact POSIX adapter
construction. Returning, capturing, containing, or forwarding imported modules
is rejected, as are all private module caches and transitive OS references.

No RED, Python, pytest, Ruff, formatter, production source, dependency,
environment, CAD, network/model access, Git mutation, TK3 work, or external
action occurred. The source remains absent.

## TK2 RP13 Static Acceptance Candidate — TK2-E023

The corrected frozen test is
`ebd2f531711e4ad1fd246e6daba60ae263c2208550ecbb89aeb25f4a2fe46b94`
at 4488 lines. It retains 82 static test shapes and 179 expected expanded
nodes. Static shell checks report no trailing whitespace, over-100-character
test line, duplicate top-level test name, extra one-second wait, or Git diff
whitespace error. Production source remains absent, and the valid RED
prediction remains one top-level missing-module import failure.

## Task Packet TK2-RP13 — Static Zero-Finding Acceptance

### 1. Anchor and authority

TK-R1/TK-A02 and TK-R2/TK-A03 authorize this read-only review. Repository:
`/Users/wangtao/Documents/DevProject/vibecad`; branch/HEAD/upstream:
`codex/task-kernel-phase2@1ef1d5df4aaa67e43ea0041a0404cdb9b3388e4a`.
Test:
`ebd2f531711e4ad1fd246e6daba60ae263c2208550ecbb89aeb25f4a2fe46b94`,
4488 lines, 82 shapes, 179 nodes. Source remains absent and dirty scope remains
artifact plus test only. Existing higher-priority rules remain binding.

### 2. Review focus and gate

Reconfirm the anchor, replay the complete TK2 contract and all findings through
E022, and begin with transitive module access, private module state, bare module
capture/forwarding, module-shaped adapter parameters, and every process-launch
family. Then repeat public API, capability, root/entry, unique authority,
exclusion, release, fork, inheritance, reflection, import-time, deadline,
platform, and residual analysis. ACCEPT requires P0/P1/P2 = 0/0/0; otherwise
report every finding and the minimum correction.

### 3. Discipline and delivery

Use only read-only static inspection. Do not execute Python, pytest, Ruff,
formatter, CAD, install, network/model access, edits, Git mutation, or TK3.
Return exact anchors, 82/179 confirmation, complete findings, RED prediction,
Windows residual, prohibited-action confirmation, and ACCEPT/REJECT.

## TK2 Focused GREEN Attempt 1 and Recovery — TK2-E030

The first approved focused GREEN command was
`PYTHONPATH=src .venv/bin/pytest -q tests/test_workflow_lease.py`. It exited 1
after 2.20 seconds with 175 passing and four failing nodes. This consumed the
first of the two permitted focused GREEN attempts. The failures were:

1. `test_path_like_resource_identifiers_are_always_hashed` attempted to capture
   an optional `os.getxattr` attribute absent from this Python/macOS build before
   exercising production behavior.
2. `test_socket_lock_entry_is_rejected` attempted to bind an AF_UNIX socket at
   a pytest temporary path longer than the platform socket-path limit before
   exercising production behavior.
3. `test_posix_open_flags_are_fail_closed` treated only the first absolute-root
   open across several independently correct root walks as valid, despite its
   later per-walk partition assertions.
4. The production fork-child cleanup used mapping `.values()`, which preserved
   runtime behavior but violated the frozen static forbidden-call policy.

These are internal recovery items within TK-R2/TK-A03 and do not change product
scope, authority, API, persistence semantics, or the TK2 allowlist. No further
user approval is required. The controller corrected the three test-harness
portability/false-red defects and replaced `.values()` with direct key
iteration plus lookup without changing cleanup semantics.

The recovery candidate is source
`b47bad0e1a63c924df750e3a8874c0f27b7fd39250e1c4a8c432dcf05555a8e8`
at 709 lines and test
`33620f8cf831bbf141896fd62d1626aefe60d20f35238c652e7332a0ccc998db`
at 4686 lines, retaining 82 static test shapes. `git diff --check`, Python
syntax compilation, the 100-character line check, and the targeted forbidden
`.values()` search are clean. One focused GREEN attempt remains. Two distinct
static reviewers must accept this recovery candidate before that final attempt.

## TK2 Recovery Static Acceptance — TK2-E031

The first recovery review found that the initial short socket root was specific
to Darwin and that setup failures were not fully covered by cleanup. A second
review found that cleanup could touch a pre-existing colliding path when root
creation failed. The controller closed both findings without changing product
behavior: the test now uses `Path(os.path.realpath("/tmp"))`, covers the complete
fixture lifecycle with `try/finally`, and guards path/root deletion with proof
that the test created the root.

Two distinct reviewers independently ACCEPT the final recovery candidate with
P0/P1/P2 = 0/0/0. The accepted source remains
`b47bad0e1a63c924df750e3a8874c0f27b7fd39250e1c4a8c432dcf05555a8e8`
at 709 lines. The accepted test is
`2559defdc3e579f060fa64434d74976388f049d7675beaefe4f55f235ec73104`
at 4692 lines with 82 static test shapes. Static syntax, whitespace,
line-length, forbidden `.values()`, ownership, lifecycle, POSIX portability,
root-walk, optional-probe, and fork-child checks are clean. The sole remaining
focused GREEN attempt is now authorized.

## TK2 Focused GREEN Attempt 2 Breaker — TK2-E032

The second focused GREEN command exited 1 after 2.32 seconds with 178 passing
and one failing node. All runtime behavior nodes passed. The remaining static
node reported only that its collected call names intersected its forbidden
call set. A read-only AST diagnostic identified the exact intersection as
`compile`, produced solely by the three approved top-level `re.compile` calls.

This exposes an internally contradictory oracle: the same static test expressly
permits exactly those three `re.compile` import-time assignments at lines
4257-4258 while its broad call-name set independently forbids every `compile`
attribute call. No production implementation can both use that explicit
allowance and satisfy the broad prohibition. The controller therefore removed
only `compile` from `forbidden_calls`; the exact `re.compile` receiver, target,
import-time, module-attribute, capture, and alias restrictions remain binding.
This is a frozen-test false-red correction, not a product or implementation
scope change.

Both originally budgeted focused GREEN attempts are consumed. No third focused
attempt is permitted. Before proceeding, two distinct static reviewers must
confirm the contradiction and minimum correction. If accepted, the next
executable gate is the already-required repository regression rather than a
third focused retry; any lease failure there is a new circuit breaker.

## TK2 Full Regression Breaker and Recovery — TK2-E033

After two independent reviewers accepted the node-sensitive `compile` oracle,
the controller ran the required full repository regression rather than a third
focused retry. It exited 1 with 1478 passing, 81 deselected, two platform fork
deprecation warnings, and one lease static-oracle failure. The failure reported
one unsafe module-object load at production line 205: the dynamic
`hasattr(os_module, name)` capability-presence loop. All runtime nodes passed.

The implementation now removes that dynamic lookup loop. Every required POSIX
flag, support set, callable, and membership remains checked through its exact
approved attribute expression; a missing exact attribute is caught as
`AttributeError` and mapped fail-closed to `LOCK_UNAVAILABLE`. This preserves
the public missing-capability behavior while satisfying the no-dynamic-module-
lookup boundary. Two distinct static reviewers must accept this recovery before
the full regression is rerun. A further unexplained lease failure remains a
circuit breaker.

## TK2 Capability Recovery Review Correction — TK2-E034

The first recovery candidate mapped `AttributeError` to `LeaseError` inside the
active exception handler. Independent review correctly found that this would
retain the native exception in `LeaseError.__context__`, violating the stable
error contract even though the visible code was correct. The handler now only
records a boolean; `LeaseError(LOCK_UNAVAILABLE)` is raised after leaving the
handler, preserving both fail-closed behavior and empty cause/context. The
candidate must retain two-reviewer zero-finding acceptance before regression.

## TK2 GREEN and Compatibility Acceptance Candidate — TK2-E035

Two independent reviewers accepted the corrected capability recovery with
P0/P1/P2 = 0/0/0. The repeated full command
`PYTHONPATH=src .venv/bin/pytest -q` then exited 0 with 1479 passing, 81
deselected, and two expected macOS deprecation warnings for the deliberate
fork-in-a-multithreaded-process tests; both warned tests passed. Runtime was
47.23 seconds.

Ruff check passed. Ruff format check initially requested a mechanical test-file
format, which the controller applied. After formatting, the focused lease suite
exited 0 with 179 passing in 6.02 seconds; Ruff check, Ruff format check, pure
module import, `git diff --check`, and the exact three-path allowlist all pass.

The final review candidate is source
`7b2a7689d29a2246f4f126c8d3c0d68756e9414ce801c51468a31ce897d55fcf`
at 691 lines and test
`ecedf0f172c3fda97b68ae0bac6e84d796d7c1340583bb0fd3fcb4f6c7eadd22`
at 4616 lines. The artifact is the only modified tracked path; source and test
are the only untracked paths. A distinct complete read-only review must return
P0/P1/P2 = 0/0/0 before named-file staging and staged gates.

## TK2 Final Independent Acceptance — TK2-E036

Two distinct complete read-only reviews ACCEPT the formatted final candidate at
P0/P1/P2 = 0/0/0. They independently reconfirm public API and stable errors,
same-process and cross-process exclusion, fork isolation, exact-owner release,
descriptor cleanup/rollback, root and entry safety, capability fail-closed
behavior, the static source boundary, and absence of material false-red or
false-green risk.

The accepted residuals remain deliberate: Windows byte-range locking has only
deterministic adapter coverage and the production selector fails closed there;
live behavior is unclaimed. POSIX operation requires the declared dir-fd,
no-follow, advisory-lock, and at-fork capabilities and fails closed when they
are unavailable. Network/distributed-filesystem durability is outside TK2.
The candidate is authorized for exact three-file staging and staged gates.

## TK2 Staged Gate Acceptance — TK2-E037

Only the three authorized TK2 paths were staged. `git diff --cached --check`
and the exact staged-name inspection passed. The staged full regression exited
0 with 1479 passing and 81 deselected in 59.89 seconds. Staged-candidate Ruff
check, Ruff format check, pure module import, and cached diff whitespace check
all passed. The named commit and non-force push are authorized.

## TK2 Closeout and TK3 Resume — TK3-E000

TK2 was committed as `612ad79c440fd2f9714485fe4ac176febc6bdde4`
(`feat(workflow): coordinate exclusive resource leases`) and pushed non-force.
On 2026-07-18 the controller reconfirmed a clean
`codex/task-kernel-phase2` branch with HEAD equal to its upstream at that exact
commit. The user then asked whether work was still progressing; no background
process existed between turns, and this message resumes the already approved
TK3 packet without expanding scope.

The current capability profile remains:

    approval: native-plan
    delegation: spawn-send-wait
    persistence: repo-artifact
    process: native-session-poll

Adapter evidence is restricted to the required categories:

- live capability declarations
  - Repository patching, sub-agent follow-up, and native command sessions are
    declared by the current Codex host; no durable native memory is declared.
- observable behavior
  - Exact staging, commit, non-force push, independent review, and native
    session polling completed for TK2 in this session history.
- environment identity
  - Codex desktop controller `/root` in
    `/Users/wangtao/Documents/DevProject/vibecad`.
- public configuration
  - Workspace filesystem access is unrestricted, network is enabled, and the
    approval policy is `never`; no escalation is required for approved scope.

## Task Packet TK3-P1 — Atomic TaskRun Persistence

### 1. Anchor and authority

TK-R1/TK-A02 explicitly approved TK3, TK-D02–TK-D07, TK-D20, TK-D24, the
nine-commit sequence, named-file scope, test-first gates, independent review,
commit, and non-force push. TK-R2/TK-A03 remains controlling for internal
fail-closed recovery discipline. Anchor:
`codex/task-kernel-phase2@612ad79c440fd2f9714485fe4ac176febc6bdde4`,
upstream-equal and clean before this artifact append. No duplicate user
approval is required.

### 2. Product outcome and contract boundary

Implement an internal local store that durably creates, loads, and
compare-and-sets immutable `TaskRun` values. `StoredTaskRun` owns a nonnegative
store generation distinct from TaskRun transition sequence. Records use one
canonical deterministic JSON envelope containing schema version, generation,
payload, and SHA-256 checksum. The store rejects malformed, duplicate-key,
non-finite, oversized, checksum-mismatched, noncanonical, or invariant-invalid
records and never returns partially decoded state.

Writes are same-directory create-exclusive temporary-file transactions with
private modes, complete write/flush/file-fsync, atomic `os.replace`, and
directory fsync. Readers observe only a complete old or complete new record.
Task identifiers are canonical and select only SHA-256-derived filenames.
Store and entry roots must be explicitly trusted, private, ordinary local
filesystem objects. TK3 does not expose MCP, touch CAD, add a database, repair
corrupt records, or unify the task and project revision durability domains.

### 3. Exact allowlist and public surface

Only these paths may change:

- `docs/orchestrated/vibecad-task-kernel-phase2.md`
- `src/vibecad/workflow/store.py`
- `tests/test_task_store.py`

The source must export `StoredTaskRun`, `TaskRunStore`, `TaskStoreError`,
`TaskStoreErrorCode`, and the explicit trusted-local root marker required by
TK-D07. Public store operations are `create`, `load`, and `compare_and_set`;
all IDs, generations, returned envelopes, and error codes use exact types.
Stable codes are `invalid_id`, `not_found`, `already_exists`, `conflict`,
`corrupt_record`, `record_too_large`, `unsafe_store`, `lock_unavailable`,
`io_error`, and `durability_uncertain`.

### 4. Test-first execution and gates

1. Run the unchanged full baseline at the clean TK2 anchor.
2. Resolve exact API and failure linearization with bounded independent
   read-only design reviews. Controller writes `tests/test_task_store.py` first.
3. Freeze an independently accepted test candidate and capture one genuine RED
   caused solely by the absent `vibecad.workflow.store` module.
4. Implement the minimum `store.py`. GREEN covers canonical round trips,
   generation CAS, process races, old-or-new reader visibility, traversal and
   Unicode IDs, root/entry/temp attacks, record cap, duplicate JSON keys,
   NaN/Infinity, checksum/truncation, permissions, every write/fsync/replace
   fault, and owned-temp cleanup.
5. Run cumulative TK1–TK3 and full repository tests, Ruff check/format, pure
   import, diff/hash/allowlist, and two distinct complete read-only reviews.
6. Stage only the three named files, rerun staged gates, commit exactly
   `feat(workflow): persist task runs atomically`, push non-force, and verify
   clean HEAD/upstream equality.

### 5. Budgets and circuit breakers

Use one genuine missing-module RED and at most two focused GREEN attempts.
Synchronous waits and race tests are bounded to five seconds. Stop execution on
an unexpected RED, second unsuccessful GREEN, deadlock, ambiguous replace/fsync
linearization, cleanup outside controller-owned temporary names, scope drift,
unexplained baseline regression, or an unsupported durability claim. Internal
test-oracle/review defects remain recoverable under TK-A03 but must be recorded
and independently re-reviewed; they do not require product approval.

### 6. Roles and process discipline

Controller owns artifact/test/source edits, serialized pytest, exact staging,
commit, and push. `spawn-send-wait` reviewers independently cover filesystem
transactions/CAS and serialization/security, followed by distinct final review.
Long-running commands retain their original native session and are polled;
they are never relaunched while active. No network/model/CAD/environment action
is part of TK3.

### 7. Delivery and residuals

Deliver one independently accepted, pushed TK3 commit plus exact gate evidence
and recovery snapshot. Preserve TK-R08 (network filesystems unclaimed), TK-R09
(same-user malicious tampering outside trust boundary), TK-R10 (TaskRun and CAD
revision stores remain separate durability domains), and TK-R11 (raw public
ingress cap deferred). Do not begin TK4 until TK3 is clean and upstream-equal.

## TK3 Baseline Evidence — TK3-E001

At clean pushed TK2 anchor
`612ad79c440fd2f9714485fe4ac176febc6bdde4`, with only this approved artifact
append in the worktree, `PYTHONPATH=src .venv/bin/pytest -q` exited 0 with 1479
passing, 81 deselected, and the same two passing macOS fork deprecation warnings
in 12.66 seconds. No TK3 source/test, dependency, environment, CAD, network,
model, Git mutation, or out-of-allowlist action occurred.

## TK3 Resolved Store Contract — TK3-E002

Two independent read-only reviews covered filesystem/CAS and serialization/
security. The controller adopts their fail-closed findings while preserving
TK-D04's approved numeric store generation (rather than substituting a content
revision token).

### API and envelope

- `TaskRunStore(root, lease_manager, *, trust)` receives an existing trusted
  0700 store root and a `ResourceLeaseManager`; it does not create directories.
- `create(task_run)`, `load(task_id)`, and
  `compare_and_set(task_id, expected_generation, task_run)` return an exact
  immutable `StoredTaskRun`. Generation starts at zero and increments by one;
  the CAS task ID and value ID must match. No upsert or caller owner token exists.
- The exact schema-v1 envelope keys are `schema_version`, `generation`,
  `task_run`, and `checksum`. Checksum is SHA-256 over a domain separator plus
  canonical JSON bytes of the other three fields. Final bytes are canonical
  UTF-8 JSON: sorted keys, compact separators, `ensure_ascii=False`, no NaN,
  whitespace, BOM, newline, or Unicode normalization.
- Exact built-in types are required. Generation is 0 through the safe JSON
  integer maximum. The final encoded record cap is 2 MiB inclusive.

### Decode and safety order

Load opens only the hashed coordinator-owned filename relative to a verified
directory FD. Size is bounded before reading/decode/hash. A pre-scan bounds JSON
nesting before parsing. Parsing rejects duplicate keys, every float and
NaN/Infinity token; post-parse validation bounds nodes and strings. Exact
envelope shape/types, canonical-byte equality, checksum, and only then
`TaskRun.from_mapping` are checked. A final `to_mapping()` equality check closes
normalization gaps. All corrupt-input paths return one fixed redacted error.

### Transaction and linearization

Every operation acquires the same canonical `task-store:<task_id>` resource
lease internally. Mutation validates/encodes before storage work, then under the
lease validates the root/final state, creates an unpredictable same-directory
0600 temp with `O_EXCL|O_NOFOLLOW|O_CLOEXEC`, performs a complete short-write
loop, file fsync and close, revalidates expected state, and atomically replaces
the final name relative to the directory FD. Replace success is the logical
linearization point; directory fsync is durability confirmation.

Before replace, failure leaves the final old/absent and removes only the exact
inode created by that operation. After replace, cleanup never removes final.
Directory-fsync or release uncertainty after replace reports
`durability_uncertain` and the committed generation; callers must load/reconcile
rather than blindly retry. Same-process and cross-process create/CAS races have
one winner because the expected-state check and replace share the lease.

Root, final, and temp checks reject symlinks, non-directories/non-regular files,
hardlinks, wrong ownership/mode, identity replacement, unsupported capabilities,
and inherited descriptors. No stale-temp scan or garbage collection exists.
Only Darwin/Linux trusted-local POSIX operation is claimed; Windows and network
filesystems remain fail-closed residuals.

## TK3 Test Review Round 1 and Correction — TK3-E003

The initial formatted test candidate was
`925a9190e558cf105a22b721f73c3d5678a8925a605db9687ce864a5f7fd1929`
at 660 lines and 29 top-level test shapes. Two independent static reviews
REJECTED at P0/P1/P2 = 0/7/3 and 0/5/3. They agreed the sole RED prediction was
the absent store module, but found checksum/semantic masking in duplicate-key,
numeric, and envelope tests; insufficient pre-decode resource, TaskRun exact
round-trip, transaction fault, CAS, lease-release, owned-temp, identity,
capability, process-race, API-shape, redaction, and source-surface coverage.

The controller corrected those internal oracle defects before RED. Adversarial
records now carry checksums valid for their precise last-wins/type/number body;
oversize and depth prove pre-decode ordering; TaskRun decode exceptions,
checksum ordering, selected-ID binding, and exact mapping round-trip are tested.
The transaction matrix now includes create/CAS preservation, shared lease key,
lease acquisition/release outcomes, exact temp flags/mode/dir-fd/inheritance,
foreign/colliding/replaced temp ownership, root replacement, platform/capability
fail-closed, reader visibility, and cross-process CAS. Signature and error shape
are exact, and an AST gate closes imports, path conveniences, broad exceptions,
and non-dir-fd replace.

The corrected candidate is
`1fa7a8bac95652df391faca2b1273186abadd2f8a7aaf1fa2b2ed9ff6a475a2e`
at 1164 lines with 50 top-level test shapes. Ruff check/format and whitespace
checks pass. Production source remains absent; no pytest or RED has run.

## TK3 Test Review Round 2 and Correction — TK3-E004

Round 2 again predicted only the missing-module RED but REJECTED at P0/P1/P2
= 0/4/2 and 0/2/4. Remaining material gaps were exact extra-field checks,
post-parse node/string/integer budgets, transaction open/partial-write/close/
revalidation/cleanup phases, CAS post-commit uncertainty, in-operation identity
replacement, complete capability profiles, and AST bypasses. P2 items covered
depth boundary, Unicode single-factor canonical forms, exact record cap,
write-side oversize, reader evidence, and broader error redaction.

The controller closed these before RED. The new matrix adds valid-checksum extra
fields; float exponent anti-masking; depth max/max+1 decode ordering; node,
string, huge-integer and parser-recursion budgets; exact-cap and encoded-cap
ordering; isolated Unicode escaping, slash escaping, and normalization; temp
open, partial-write, close, cleanup and create/CAS revalidation failures; root
and final replacement during operations; CAS directory-fsync/release committed
generation; every required POSIX storage capability, exact types and membership;
directory/final FD call traces; and exact OS/import/dynamic/broad-handler AST
surfaces. Reader evidence now contains known old and new snapshots.

The Round 3 candidate is
`afd0827052bc854b5eb72a8335df3bfbe03b2d7c9409ba010e688caf42bd733b`
at 1753 lines with 70 top-level test shapes. Ruff check/format and whitespace
checks pass. Source remains absent and no pytest/RED has occurred.

## TK3 Test Review Round 3 and Correction — TK3-E005

Round 3 REJECTED at P0/P1/P2 = 0/2/0 and 0/2/1 while preserving the sole
missing-module RED prediction. The narrow remaining issues were cleanup after a
temp-close error, precommit primary-vs-release error precedence, runtime root/
final/temp ownership and mode, string-aware depth scanning, exact identifier/
generation subclasses, and explicit resource-budget boundaries.

The controller added exact temp cleanup assertions, a precommit release-failure
precedence case, runtime chmod and wrong-uid tests for root/final/temp, valid
strings containing quotes/backslashes and more than 64 bracket characters,
canonical str/int subclass rejection before storage, and exact 8192-node /
65536-byte-string helper boundaries. The Round 4 candidate is
`521a14182c569c28561467ec9338f70368a3dd6c99c790ebb62c7679c3ad863a`
at 1894 lines with 76 top-level test shapes. Ruff and whitespace gates pass;
source remains absent and RED has not run.

## TK3 Test Review Round 4 Final Correction — TK3-E006

One reviewer ACCEPTED Round 4 at 0/0/0. The independent serialization reviewer
found one P1 and one P2: exact TaskRun subclass rejection was not exercised,
and total JSON depth jumped from 64 to 66 rather than testing 65. The controller
added an adversarial TaskRun subclass whose `to_mapping` must never execute and
requires StoredTaskRun/create/CAS rejection before storage, and changed the
pre-decode depth rejection to exactly envelope-plus-64 arrays (total 65).

The final candidate is
`cb8670180dcc699d924c3f439fe337c3b9dd9c4742d5d46f81882f2571e7eeb7`
at 1931 lines with 77 top-level test shapes. Ruff check/format and whitespace
checks pass. Source remains absent; the expected sole RED remains a top-level
missing-module collection error.

## TK3 Frozen-Test Static Acceptance — TK3-E007

Two distinct reviewers ACCEPT the final frozen test at P0/P1/P2 = 0/0/0 and
confirm the exact SHA, 1931 lines, 77 test shapes, source absence, and sole
missing-module RED prediction. The test is frozen. One genuine RED is now
authorized; production source may be created only if collection fails solely
because `vibecad.workflow.store` is absent.

## TK3 Genuine RED Evidence — TK3-E008

`PYTHONPATH=src .venv/bin/pytest -q tests/test_task_store.py` exited 2 in 0.32
seconds with exactly one collection error: `ModuleNotFoundError: No module named
'vibecad.workflow.store'` at the frozen top-level import. No test body executed
and no secondary failure appeared. This is the accepted genuine RED. The
minimum production implementation is authorized; two focused GREEN attempts
remain.

## TK3 Pre-GREEN Implementation Review and Controlled Test Reopen — TK3-E009

The first production candidate implemented the complete frozen contract and
passed Ruff check/format, Python compilation, and whitespace checks without
running pytest. Independent static review found one guaranteed fixture failure:
`pathlib.Path(...)` produces the platform's concrete `PosixPath`, so an exact
`Path` class check rejected every valid store root. The controller corrected
root coercion to accept only exact `str` or the current platform's exact
`type(Path("/"))`, preserving subclass and generic `PathLike` rejection.

A proposed compatibility fix then validated import-time native `open`, `stat`,
and `unlink` membership instead of the active callables. Independent review
REJECTED this as a P1 false-green: a replaced callable could lack or ignore
`dir_fd`/`follow_symlinks` while the cached native primitive made the gate pass.
The controller discarded that production change and retained active-callable
membership checks.

The accepted RED remains genuine, but the previously frozen test had a related
oracle defect: seven legitimate runtime `open`/`unlink` wrappers did not declare
their supported `dir_fd` capability. Under TK-A03 internal-repair authority, the
controller performed a narrow controlled reopen. A test-only helper now copies
the exact-set capability registry, adds the wrapper being exercised, and then
installs it; invalid-input probes and missing-capability/membership tests remain
unchanged. No test body or product contract was weakened.

The corrected anchors are production
`4978f1a72d7029110704533c2f1ce26aea9036975cd115709f6a75b533c255c5`
at 842 lines and test
`8e58d29a9760d7ce9dd3c267bc7ae0d4e9c735744f21378d215e211fcf6a9f8a`
at 1938 lines. Ruff check/format, Python compilation, and whitespace checks all
pass. The test is re-frozen at this anchor pending independent static
acceptance. No focused GREEN attempt has run; both attempts remain.

## TK3 Re-Freeze Acceptance and Focused GREEN Attempt 1 — TK3-E010

Two independent static reviewers ACCEPTED the controlled capability-oracle
correction at source
`4978f1a72d7029110704533c2f1ce26aea9036975cd115709f6a75b533c255c5`
and test
`8e58d29a9760d7ce9dd3c267bc7ae0d4e9c735744f21378d215e211fcf6a9f8a`.
They confirmed all seven wrappers that reach the storage capability gate declare
their active callable membership, while invalid-input ordering probes and every
missing/invalid/membership-negative check remain unmodified and fail closed.

Focused GREEN attempt 1,
`PYTHONPATH=src .venv/bin/pytest -q tests/test_task_store.py`, exited 1 in 2.29
seconds with 157 passing and exactly one failure:
`test_precommit_failures_preserve_old_or_absent_record_and_cleanup_temp[fsync]`
expected task-temp `IO_ERROR` but observed `LOCK_UNAVAILABLE`. The generic
`os.fsync` injection affected the shared `os` module and therefore failed the
fresh lease root's directory fsync during first acquisition, before any task
store mutation. This is an execution-order oracle defect, not a production
failure; all other focused cases passed.

The controller narrowed only that test injection: directory descriptors delegate
to the captured real fsync, while a regular-file descriptor raises the intended
precommit fault. Production remains unchanged. The corrected test is
`2a930b94de1d72650ebbec23f9ca70c7f8c77b7fb4126d381544a186529e183d`
at 1946 lines; Ruff check/format, Python compilation, and whitespace checks pass.
Independent static acceptance is required before focused GREEN attempt 2. One
focused attempt remains.

## TK3 Focused GREEN Acceptance — TK3-E011

Two independent reviewers ACCEPTED the narrowed precommit-fsync oracle after
re-reading its exact execution path: the fresh lease-root directory fsync
delegates to the real primitive, the task temp regular-file fsync raises, the
store reports precommit `IO_ERROR`, replace is skipped, and the owned temp is
removed by inode identity. No directory-durability behavior is masked.

Focused GREEN attempt 2,
`PYTHONPATH=src .venv/bin/pytest -q tests/test_task_store.py`, exited 0 in 2.20
seconds with all 158 cases passing (pytest reported 1.59 seconds). Both focused
attempts are now consumed and the TK3 focused contract is GREEN at production
`4978f1a72d7029110704533c2f1ce26aea9036975cd115709f6a75b533c255c5`
and test
`2a930b94de1d72650ebbec23f9ca70c7f8c77b7fb4126d381544a186529e183d`.
Full regression and delivery gates remain required.

## TK3 Full Regression and Unstaged Delivery Gates — TK3-E012

The full repository command `PYTHONPATH=src .venv/bin/pytest -q` exited 0 in
15.17 seconds with 1637 passing, 81 deselected, and the same two expected macOS
fork deprecation warnings; both warned lease tests passed. This is the clean
cumulative TK1–TK3 regression.

Repository-wide `ruff check .`, pure `vibecad.workflow.store` import with exact
`__all__`, `git diff --check`, candidate hashes, and the exact three-path status
allowlist pass. Exact candidate format check for `store.py` and
`test_task_store.py` passes. A diagnostic `ruff format --check .` also reported
74 legacy files outside the TK3 allowlist that the repository's current Ruff
version would reformat; none is modified, and TK3 does not claim or expand into
a repository-wide formatting migration. The candidate-scoped format gate used
by prior Stage C deliveries remains authoritative.

The final unstaged review anchors remain production
`4978f1a72d7029110704533c2f1ce26aea9036975cd115709f6a75b533c255c5`
at 842 lines and test
`2a930b94de1d72650ebbec23f9ca70c7f8c77b7fb4126d381544a186529e183d`
at 1946 lines. Only the approved artifact is modified and those two approved
paths are untracked. Two distinct complete read-only reviews are now required
before staging.

## TK3 Final Review Rejection and Ownership Repair — TK3-E013

Both required final reviewers independently REJECTED the previously GREEN
candidate at P0/P1/P2 = 0/1/0. The shared P1 was an uncovered resource-lifecycle
failure: successful opens were followed by unguarded `fstat` and
`get_inheritable` calls, while mutation's initial record read occurred before
its cleanup state. A native metadata error could escape as raw `OSError`, skip
lease release, leak root/final/temp descriptors, and leave an owned temp. Even a
typed corrupt/unsafe initial record in CAS leaked the mutation root descriptor.
Cleanup-path stat failure could also replace the primary error and skip root
close. Passing focused/full suites therefore did not authorize staging.

Under TK-A03 internal-repair authority, the controller performed a bounded
ownership rewrite without changing the public API, record schema, or replace
linearization point. Exact checked helpers now map `fstat`, inheritability, and
effective-UID probe failures. Root walking and final-record reads close every
descriptor they own before returning a typed error. Mutation initializes root,
temp, identity, replace, and failure state before its first read; every
precommit stage converges on one cleanup tail, identity-unknown temps are never
unlinked by name, cleanup stat/unlink cannot replace the primary failure, and
replace-success failures still promote only to `durability_uncertain` with the
new generation. Load and mutation release their acquired lease in a nonthrowing
`finally`, while user errors are constructed only after handlers/finalizers exit
so cause/context remain empty.

New static oracles cover root/final first and second `fstat`, root/final
inheritability, temp `fstat` before identity, temp inheritability after identity,
typed corrupt initial CAS read, native initial-read `OSError`, and cleanup-stat
failure. They assert exact redacted codes, descriptor closure, old/absent final
state, identity-safe temp behavior, and a subsequent normal operation proving
lease release. One reviewer first found that corrupt JSON alone did not exercise
native `os.read` failure; the controller added that narrow case before any
repair execution.

The pre-execution repair anchors are production
`a330bc170c589070b8ef0eb22a01e4a38789c3dfdaa91bc7603c55f9f9a33888`
at 900 lines and test
`910c08c018eee410e146a18623d1662c4b42a4511e569c69822f90dd05110cd4`
at 2189 lines. Ruff check/format, Python compilation, and whitespace checks pass.
No pytest has run against the repair. Because both focused GREEN attempts are
already consumed, the independently accepted repair proceeds only to the full
repository regression; any failure reopens recovery rather than creating a
third focused attempt.

## TK3 Ownership-Repair Full Regression Failure and Recovery — TK3-E014

After two-reviewer static acceptance, the ownership-repair full command
`PYTHONPATH=src .venv/bin/pytest -q` exited 1 in 16.41 seconds with 1644 passing,
81 deselected, four failures, and three warnings. The first failure was the task
store concurrent-reader test: the main CAS raced the reader's valid exclusive
lease and received the contractually allowed `lock_unavailable`, but the test
incorrectly required immediate writer success. Its abrupt exit skipped the
reader stop/join sequence. The still-running reader then crossed into later
lease tests while they monkeypatched the shared process registry and OS probes,
producing one thread `KeyError` warning and three downstream lease failures
(double-observed close, unresolved path trace, and mismatched adapter registry
key). Those three are causally explained contamination, not independent product
regressions.

The concurrency oracle now accepts only that one allowed contention outcome,
stops and joins the reader, retries CAS after lease release, and unconditionally
stops/joins in `finally` on every path. It still begins with known generation 0,
records only complete 0/1 reads, and ends with generation 1, while preventing a
failed assertion from contaminating subsequent tests.

A parallel final residual audit found one P2 input edge: an exact root string or
Path containing NUL (or another native-encoding-invalid component) reaches
component `os.open`, whose `ValueError` was not mapped and could skip parent-FD
cleanup. Root opens now map exact `OSError`, `TypeError`, and `ValueError` into
the existing fail-closed state. A new str/Path constructor oracle verifies
fixed redacted `unsafe_store` and closure of every recorded walk descriptor.

The recovery candidate is production
`73c1588872719b8b1bff59d34e9fd0e0e42d47aa9d2016121fd5ae18e7e6be6c`
at 900 lines and test
`1a20347d313c351adfb33d0b75f7128c2b109c9f3b7152c6ed864394ce57d165`
at 2227 lines. Ruff check/format, Python compilation, and whitespace checks pass.
No pytest has run against this recovery candidate; independent static review is
required before repeating the full regression.

## TK3 Concurrency-Oracle Strengthening — TK3-E015

A stricter independent review accepted the NUL-root fix but rejected the first
concurrency recovery at P0/P1/P2 = 0/1/1. A reader-side non-TaskStore exception
could still terminate as a pytest thread warning without entering `unexpected`,
and the contention fallback could stop the reader before the successful CAS,
making its concurrent evidence vacuous.

The oracle now requires a successful reader-owned generation-0 load before the
writer starts. Every ordinary reader exception is captured into `unexpected`
and signals termination. If the writer first receives the allowed contention
code, it asks the reader to yield and waits until the reader reaches a bounded
pause only after its current load and lease release; the CAS then commits while
the reader thread remains alive. The same reader resumes and must successfully
observe generation 1. Every wait is bounded to five seconds, and `finally`
always clears yield, stops, and joins the reader. This both prevents cross-test
pollution and provides non-vacuous pre/post transition reader evidence.

Independent static re-review ACCEPTED the strengthened schedule at P0/P1/P2 =
0/0/0. The current recovery anchors are production
`73c1588872719b8b1bff59d34e9fd0e0e42d47aa9d2016121fd5ae18e7e6be6c`
at 900 lines and test
`f8ab294d38019c1cff12109e80914200f14ff047e487a1bea22fc2042a8462dc`
at 2248 lines. Static gates pass. A repeated full regression is authorized.

## TK3 Ownership-Recovery Full GREEN — TK3-E016

The repeated full repository command `PYTHONPATH=src .venv/bin/pytest -q`
exited 0 in 15.77 seconds with 1649 passing, 81 deselected, and only the two
known passing macOS fork deprecation warnings (pytest reported 14.62 seconds).
The prior concurrency root failure, thread warning, and all three contaminated
lease failures disappeared together, confirming the recorded causal diagnosis.

The final test count adds twelve ownership/boundary cases to the previously
focused 158: root/final/temp metadata failures, typed and native initial-read
recovery, cleanup-stat precedence, and invalid native root input. Public
behavior, serialization, CAS, process races, and all pre-existing repository
tests remain GREEN. Final static gates and two fresh complete read-only reviews
remain required before staging.

## TK3 Final Independent Acceptance — TK3-E017

Two distinct fresh complete read-only reviews ACCEPT the final recovery
candidate at P0/P1/P2 = 0/0/0. They independently reconfirm exact public API and
redacted errors; bounded canonical serialization; trusted-root and active
capability gates; root/final/temp FD ownership; identity-safe cleanup; lease
release; create/CAS/read concurrency; replace and durability linearization; the
strengthened non-vacuous reader schedule; AST surface; and every earlier
rejection's closure. Both also verify the artifact's historical failure and
recovery evidence and the exact three-path allowlist.

The accepted source/test anchors are
`73c1588872719b8b1bff59d34e9fd0e0e42d47aa9d2016121fd5ae18e7e6be6c`
at 900 lines and
`f8ab294d38019c1cff12109e80914200f14ff047e487a1bea22fc2042a8462dc`
at 2248 lines. The full recovery regression is 1649 passing with 81 deselected;
whole-repository Ruff check, candidate format, compilation, pure import,
whitespace, hash, and status gates pass. Exact three-file staging and staged
delivery gates are authorized.

## TK3 Staged Gate Acceptance — TK3-E018

Only the three authorized TK3 paths were staged, with no unstaged path.
`git diff --cached --check` and the exact staged-name inspection passed. The
staged full regression exited 0 in 15.03 seconds with 1649 passing, 81
deselected, and the same two known passing macOS fork warnings (pytest reported
13.76 seconds). Staged-candidate whole-repository Ruff check, candidate format,
pure module import, cached whitespace, and source/test hash gates all passed.

The staged source/test anchors remain
`73c1588872719b8b1bff59d34e9fd0e0e42d47aa9d2016121fd5ae18e7e6be6c`
and `f8ab294d38019c1cff12109e80914200f14ff047e487a1bea22fc2042a8462dc`.
The exact commit `feat(workflow): persist task runs atomically` and non-force
push are authorized.

## TK3 Closeout and TK4 Resume — TK4-E000

TK3 was committed as `5883f7d4adf2d1c5617ea594bdf400ddf1c3f362`
(`feat(workflow): persist task runs atomically`) and pushed non-force. The
controller verified a clean `codex/task-kernel-phase2` branch with HEAD equal to
its upstream at that exact commit. Under TK-A02/TK-A03 and the user's standing
instruction to continue without internal product approvals, the already
approved ordered TK4 packet now begins. No architecture, public MCP, model,
environment, CAD, or dependency scope is expanded.

## Task Packet TK4-P1 — Deterministic Acceptance Verification

### 1. Anchor and authority

TK-R1/TK-A02 explicitly approved TK4, TK-D13–TK-D17, TK-D24, the nine-commit
sequence, named-file scope, genuine RED, independent review, exact commit, and
non-force push. TK-R2/TK-A03 remains controlling for internal oracle/review
repair. Anchor:
`codex/task-kernel-phase2@5883f7d4adf2d1c5617ea594bdf400ddf1c3f362`,
upstream-equal and clean before this artifact append. No duplicate user approval
is required.

### 2. Product outcome and contract boundary

Implement a pure deterministic acceptance verifier that compiles the existing
strict `AcceptanceSpec` into an authentic internal capability and verifies it
only against a trusted immutable `ObservationSnapshot`. The verifier returns
the durable `CriterionVerdict` values and `VerificationReport` already defined
by TK1, plus an opaque receipt bound to candidate revision, manifest SHA-256,
observation digest, and compiled spec identity.

The closed TK-D15 allowlist is geometry volume, area, bbox, and center of mass;
topology valid shape and solid count; and artifact exists, non-empty, and
format. Checks use explicit canonical units and exact scalar/vector tolerance
rules. Required failures or unsupported criteria fail verification; optional
unsupported criteria remain diagnostic and cannot independently authorize an
empty or pure-visual specification. Step results, execution acknowledgements,
untrusted mappings, dynamic callables, and model output can never satisfy a
criterion. TK4 reads no files, invokes no CAD, exports nothing, mutates no
project/task state, and performs no network/model/MCP operation.

### 3. Exact allowlist and public surface

Only these paths may change:

- `docs/orchestrated/vibecad-task-kernel-phase2.md`
- `src/vibecad/validation/__init__.py`
- `src/vibecad/validation/contracts.py`
- `src/vibecad/validation/checks.py`
- `src/vibecad/validation/engine.py`
- `tests/test_acceptance_verifier.py`

The public validation package must expose strict immutable shape/artifact/
snapshot observations, compiled acceptance and receipt types, stable redacted
validation errors, `compile_acceptance_spec`, and `verify_acceptance`. Exact
constructor and mapping surfaces, check names, units, tolerance semantics,
digest domains, receipt authenticity, and report binding are frozen in tests
before production modules exist. No dependency, compatibility shim, registry,
plugin discovery, filesystem path, or public server/MCP surface is added.

### 4. Test-first execution and gates

1. Run the unchanged full baseline at the clean TK3 anchor.
2. Resolve the exact observation, compilation, check, error, digest, and receipt
   contract with two bounded independent read-only design reviews. The
   controller writes `tests/test_acceptance_verifier.py` first.
3. Freeze an independently accepted test candidate and capture one genuine RED
   caused solely by absent `vibecad.validation` modules.
4. Implement only the four validation modules. Focused GREEN covers every
   allowlisted check, pass/fail/exact boundary, required/optional unsupported,
   bool-as-number, non-finite values, unit/vector/target ambiguity, duplicate or
   missing facts, empty/pure-visual specs, revision/digest mismatch, and forged,
   replayed, or mismatched receipts. It proves StepResult and
   `execution_acknowledged` evidence are unusable.
5. Run cumulative Stage C and the full repository suite, whole Ruff check,
   candidate format, pure import, dependency/AST/diff/hash/allowlist gates, and
   two fresh complete read-only reviews.
6. Stage only the six named files, rerun staged gates, commit exactly
   `feat(validation): verify deterministic acceptance criteria`, push
   non-force, and verify clean HEAD/upstream equality before TK5.

### 5. Budgets and circuit breakers

Use one genuine missing-module RED and at most two focused GREEN attempts.
Design-test corrections occur before RED whenever possible. Stop on an
unexpected RED, second unsuccessful GREEN, mutable/authentication state that
cannot be bounded, ambiguous tolerance or unit semantics, any filesystem/CAD/
network/model import or side effect, dynamic callable lookup, scope drift,
unexplained baseline regression, or a receipt that can be forged from public
data. Internal oracle/review defects remain recoverable under TK-A03 only when
recorded and independently re-reviewed; they require no product decision.

### 6. Roles and process discipline

The controller owns artifact/test/source edits, serialized executable
validation, exact staging, commit, and push. Independent reviewers separately
cover contracts/check semantics and authenticity/security, followed by two
fresh final complete reviews. Review workers use static read-only shell only;
they do not run Python, pytest, Ruff, formatters, CAD, network/model services,
or mutate files/Git. No long-running command is relaunched while its original
session is active.

### 7. Delivery and residuals

Deliver one independently accepted, pushed TK4 commit plus exact gate evidence
and a clean recovery snapshot. Preserve unsupported preservation, assembly,
feature inference, visual verification, implicit unit conversion, and any
filesystem-backed evidence collection for later packets. Receipt authenticity
is process-local capability authenticity for trusted same-process handlers, not
a cross-service cryptographic credential. Do not begin TK5 until TK4 is clean
and upstream-equal.

## TK4 Baseline Evidence — TK4-E001

At clean pushed TK3 anchor
`5883f7d4adf2d1c5617ea594bdf400ddf1c3f362`, with only the approved TK4
artifact append in the worktree, `PYTHONPATH=src .venv/bin/pytest -q` exited 0
in 15.04 seconds with 1649 passing, 81 deselected, and the same two known
passing macOS fork deprecation warnings (pytest reported 13.79 seconds). No TK4
source/test, dependency, environment, CAD, network, model, staging, or Git
mutation occurred.

## TK4 Resolved Validation Contract — TK4-E002

Two bounded read-only design reviews independently resolved the TK4 check
semantics and the capability-authenticity boundary before the test candidate
was written. The controller adopts a closed public package consisting of
`ValidationErrorCode`, `ValidationError`, immutable `ShapeObservation`,
`ArtifactObservation`, and `ObservationSnapshot` values, opaque
`CompiledAcceptance` and `VerificationReceipt` capabilities, an immutable
`VerificationResult`, and the three functions `compile_acceptance_spec`,
`verify_acceptance`, and `consume_verification_receipt`. Receipt consumption is
included now because TK-D17 requires later workflow transitions to distinguish
an authentic verifier success from a publicly constructible report; deferring
the consuming boundary would force TK6 to reopen the frozen TK4 allowlist.

The exact supported pairs are `geometry.volume`, `geometry.area`,
`geometry.bbox`, `geometry.center_of_mass`, `topology.valid_shape`,
`topology.solid_count`, `artifact.exists`, `artifact.non_empty`, and
`artifact.format`. Every supported criterion has an explicit target. Geometry
criteria require exactly one canonical `unit` parameter: `mm^3` for volume,
`mm^2` for area, and `mm` for bbox and center of mass. Numeric equality is
inclusive `abs(observed - expected) <= tolerance`; omitted tolerance is zero,
and the existing scalar-only criterion tolerance is broadcast over every
vector component. Boolean, integer-count, and format checks are exact and
forbid tolerance and parameters. Bbox is the existing three-axis size vector
`(x_length, y_length, z_length)`, not six placement extrema; center of mass is
the three-axis position vector. Supported output formats are the canonical
tokens `step` and `fcstd`.

An unknown required kind/check pair is a compile error. An unknown optional
pair compiles only as an `unsupported` diagnostic, and a specification must
still contain at least one supported machine criterion. Empty, pure-visual,
and optional-unsupported-only specifications are rejected. A supported target
or fact absent from a valid snapshot produces an `unsupported` verdict rather
than an exception; the existing report invariant therefore fails required
missing facts while preserving optional diagnostics. Known mismatches always
produce `fail`, independent of the `required` flag. Verdict order is source
criterion order and evidence uses stable canonical pointers into sorted,
unique shape and artifact tuples.

Observation values use exact, finite Python scalar types, reject booleans as
numbers, require canonical revision and digest syntax, and enforce bounded
identifier, tuple-count, fact-count, and canonical-byte budgets. There are at
most 128 shape and 128 artifact observations, 128 criteria, 256 UTF-8 bytes per
identifier, 64 KiB of canonical snapshot JSON, and 256 KiB of canonical
acceptance JSON. Snapshot and compiled-spec digests use canonical JSON plus
the separate `vibecad-observation-snapshot-v1\0` and
`vibecad-compiled-acceptance-spec-v1\0` domain separators.
The verifier accepts only an exact `ObservationSnapshot`; it has no adapter for
`StepResult`, execution facts, `ExecutionEvidence`, mappings, proxies, or
`execution_acknowledged`. It performs no filesystem, CAD, MCP, model, network,
dynamic import, registry discovery, or caller-supplied invocation.

Compiled and receipt instances are issued only by a private process-local
issuer, registered by exact object identity under a lock, and reject public
construction, subclassing, mutation, copying, deep-copying, and pickling. A
success receipt binds the exact compiled object, compiled-spec digest,
acceptance ID, candidate revision, manifest SHA-256, observation digest, and
the exact deterministic report. Consumption is atomic and one-shot; forged,
cross-compiled, swapped-binding, and replayed receipts fail closed. This is a
trusted same-process capability boundary, not a cross-service cryptographic
credential and not a claim against hostile native-memory or arbitrary Python
reflection.

One pre-existing upstream residual is recorded without scope expansion:
`VerificationReport.from_mapping()` omits `candidate_revision` from its
declared required-field set even though the constructor requires it. TK4
constructs reports directly and does not compensate for or edit that TK1
contract defect.

## TK4 Accepted Test Candidate — TK4-E003

The controller wrote the complete TK4 test candidate before any validation
production module existed. Its final pre-RED anchor is
`ef4541abe8486bd92045acbc90df959555cbd43e7092fafaea975b6a84bef34f`
at 1922 lines. Ruff check/format, Python compilation, and whitespace gates pass;
no pytest has run against it.

Two independent complete read-only reviews ACCEPT the candidate at P0/P1/P2 =
0/0/0. Review-driven corrections froze literal canonical snapshot and compiled
spec digest vectors and separate domains; pass/fail and inclusive/exclusive
boundaries for all nine checks; componentwise vector tolerance; every missing
fact family; exact tuple/JSON mapping boundaries; shape, artifact, criterion,
UTF-8, fact, and canonical-byte budgets; hostile-text redaction; snapshot and
observation subclass/proxy/tamper rejection; exact per-file module-and-symbol
import allowlists; dynamic-call and broad-exception AST gates; compiled and
receipt constructor/copy/pickle/reduce/replace/slot/seal attacks; changed-spec
and equal-report binding; atomic replay; bounded concurrency waits; and weak
capability lifecycle cleanup.

The accepted bbox contract is the repository's existing three-axis size vector
`(x_length, y_length, z_length)`. The accepted test collection also ensures
values rejected by the upstream workflow constructor are injected only after a
valid construction, so the authorized RED can have exactly one cause: the
absent `vibecad.validation` package. The genuine focused RED is now authorized.

## TK4 Genuine Missing-Module RED — TK4-E004

With the accepted test anchor still the only TK4 code candidate,
`PYTHONPATH=src .venv/bin/pytest -q tests/test_acceptance_verifier.py` exited 2
in 0.78 seconds during collection. The sole error is
`ModuleNotFoundError: No module named 'vibecad.validation'` at the test's public
package import; pytest reported one collection error in 0.34 seconds. There are
no assertion failures, alternate import failures, test-construction errors, or
production modules present. This is the one authorized genuine RED. The four
named validation modules may now be implemented, with no further RED run.

## TK4 Pre-GREEN Repair and Static Acceptance — TK4-E005

The four named production modules were implemented without executing the test
suite. Independent pre-GREEN reviews rejected early static candidates and the
controller repaired the findings under TK-A03 before consuming a focused GREEN
attempt. The repairs add exact spec/criterion schema and kind revalidation;
stable translation of reflected cyclic/deep workflow values; explicit live
budgets of 256 compiled capabilities and 256 receipts with weak-reference
capacity recovery; and full-report receipt binding.

The report-binding repair is value-isolating rather than alias-based. Receipt
issuance hashes the public report then reconstructs every verdict and the
report into an issuer-owned private copy before the result escapes. Consumption
revalidates the complete public report digest under the receipt lock, atomically
marks the receipt consumed, and returns the previously unexposed distinct-equal
private report. Pre-consume mutation is rejected; post-consume mutation of the
public alias cannot change the authenticated returned value.

Review-driven oracle additions cover cyclic parameters and optional expected
values, reflected schema/kind/check corruption, full report/verdict mutation,
private-copy isolation, and exact registry capacity/recovery. An exact AST
oracle correction admits only `object.__new__`, `object.__setattr__`,
`object.__delattr__`, and zero-argument `super().__init__`, without opening
other dunder calls.

Two complete final static reviews ACCEPT the pre-GREEN candidate at P0/P1/P2 =
0/0/0. Current anchors are test
`c4962e1ba4d207a576b12b61d6a4c2bc66e0f457fe28b627ce143b12850f2d7e`
at 2082 lines, contracts
`f6ab974cd125630aad8a3a7de79e04f1328e9529f241eac83e7567f0da7990e4`,
checks `c5934b361b5eabee3ba7285cfc33ebf6174174ce0c68733f52e2771f534e7e6e`,
engine `5d587feb52f355dcf2c10ee618750bacbffcc50d09c7d6b5b2ba6526baef46f9`,
and package init
`73050031b2e173739e99cb4db59c44863eea2015fac1626f8ebf76a1666a836f`.
Ruff check/format, Python compilation, and whitespace gates pass. The first of
at most two focused GREEN attempts is authorized.

## TK4 First Focused GREEN — TK4-E006

The first focused command
`PYTHONPATH=src .venv/bin/pytest -q tests/test_acceptance_verifier.py` exited 0
in 1.02 seconds with all 108 tests passing (pytest reported 0.59 seconds).
This single successful attempt exercises the nine-check matrix, exact digest
vectors, deterministic reports, stable malformed-input handling, observation
and spec budgets, StepResult/evidence exclusion, snapshot and capability
tamper resistance, full-report private-copy receipts, atomic replay,
concurrency, registry bounds/recovery, and the closed import/dynamic-call
surface. No second focused GREEN attempt is required or authorized unless a
later cumulative/full regression proves a distinct defect.

## TK4 Cumulative Stage C GREEN — TK4-E007

The cumulative TK1–TK4 plus Phase-1 workflow command covering workflow
contracts, program validation, result normalization, execution adapter, task
state, leases, task store, and acceptance verification exited 0 in 5.98
seconds with 1194 passing and one deselected test (pytest reported 5.19
seconds). Durable state/report compatibility, exact execution-evidence
separation, lease/store behavior, and every new validation path remain GREEN.
The unchanged full repository regression is authorized.

## TK4 Full Repository GREEN — TK4-E008

The full repository command `PYTHONPATH=src .venv/bin/pytest -q` exited 0 in
16.30 seconds with 1757 passing, 81 deselected, and only the two known passing
macOS multi-threaded-fork deprecation warnings (pytest reported 15.01
seconds). The 108 new deterministic-verification tests account exactly for the
increase from the clean TK3 baseline of 1649 passing tests. No pre-existing
CAD tool, runtime, MCP, feedback, execution, workflow, lease, persistence, or
platform behavior regressed. Whole-repository static gates and two fresh final
complete read-only reviews remain required before staging.

## TK4 Post-Full Controller Repair — TK4-E009

A controller audit after the first full GREEN found one bounded-diagnostics
edge case: an otherwise valid criterion could supply an overlong, control-
character-bearing, or RFC 6901-significant parameter key, and the validation
error path could echo that untrusted key or exceed the frozen path budget.
TK-A03 authorizes this internal correctness repair without a product decision.

The narrow repair rejects non-exact-string keys before sorting or comparison,
uses the fixed `/criteria/N/parameters` parent for unsafe or overlong keys,
escapes printable `~` and `/` keys as `~0` and `~1`, and requires geometry
units to be exact strings. New oracles cover overlong and multiline keys,
canonical pointer escaping, and reflected mixed-key mappings while preserving
fixed redacted messages.

Ruff check/format, Python compilation, and whitespace gates pass. Two
independent narrow pre-execution reviews verified production hash
`1d9d15d04870eefd8f10342e5bf44459567266eade023830bbc2e7c9be5f0e67`
for `checks.py` and test hash
`824d0b7e750d0ce07121776d5721d200d75a480a54ee8fe6ae318da09ec1a07a`;
both ACCEPT at P0/P1/P2 = 0/0/0. The other production anchors are unchanged.
The second and final allowed focused GREEN is authorized, followed by fresh
cumulative and full regressions because this repair reopens the candidate.

## TK4 Second Focused GREEN — TK4-E010

The second and final focused command
`PYTHONPATH=src .venv/bin/pytest -q tests/test_acceptance_verifier.py` exited 0
in 1.06 seconds with all 109 tests passing (pytest reported 0.61 seconds).
The four new hostile-parameter-key cases close the controller finding without
regressing the previously accepted deterministic-verification surface. No
further focused execution is authorized or needed.

## TK4 Reopened Cumulative Stage C GREEN — TK4-E011

The fresh cumulative TK1–TK4 plus Phase-1 workflow command exited 0 in 6.92
seconds with 1195 passing and one deselected test (pytest reported 5.85
seconds). The increase of one test from TK4-E007 is exactly the new grouped
parameter-key regression oracle; all durable-state, execution-evidence,
lease/store, and validation contracts remain GREEN.

## TK4 Reopened Full Repository GREEN — TK4-E012

The fresh full repository command `PYTHONPATH=src .venv/bin/pytest -q` exited 0
in 16.27 seconds with 1758 passing, 81 deselected, and only the same two known
passing macOS multi-threaded-fork deprecation warnings (pytest reported 15.03
seconds). The total is the clean TK3 baseline plus all 109 TK4 tests. No
pre-existing CAD tool, runtime, MCP, feedback, execution, workflow, lease,
persistence, or platform behavior regressed. Whole-repository static gates
and two fresh complete final reviews are now authorized.

## TK4 Reopened Static Gate Acceptance — TK4-E013

Whole-repository Ruff check, candidate format, Python compilation, exact pure
package import, and whitespace gates pass after TK4-E012. One controller gate
invocation initially supplied an incorrect locally written expected-name set
to the pure-import assertion; it named TK1 report types instead of the frozen
TK4-E002 public values. No source or test changed. The controller reconciled
the assertion to the exact accepted 11-name tuple and reran the complete
static gate under fail-fast execution successfully.

Final production/test anchors are package init
`73050031b2e173739e99cb4db59c44863eea2015fac1626f8ebf76a1666a836f`,
contracts
`f6ab974cd125630aad8a3a7de79e04f1328e9529f241eac83e7567f0da7990e4`,
checks
`1d9d15d04870eefd8f10342e5bf44459567266eade023830bbc2e7c9be5f0e67`,
engine
`5d587feb52f355dcf2c10ee618750bacbffcc50d09c7d6b5b2ba6526baef46f9`,
and tests
`824d0b7e750d0ce07121776d5721d200d75a480a54ee8fe6ae318da09ec1a07a`.
Only the six authorized TK4 paths are modified or untracked. Two fresh
complete final read-only reviews are authorized against these exact anchors.

## TK4 Final Independent Acceptance — TK4-E014

Two distinct fresh complete read-only reviews independently ACCEPT the reopened
final candidate at P0/P1/P2 = 0/0/0. Both verified all six authorized anchors,
the nine-check deterministic semantics, exact public surface, canonical units
and tolerances, every type and resource budget, fixed redacted diagnostics,
closed import/dynamic-call surface, and non-vacuous hostile-parameter tests.

The reviews separately reconfirm authentic compiled capabilities; exact
identity and weak-registry lifecycle bounds; complete receipt/spec/candidate/
manifest/observation/report binding; issuer-owned private report isolation;
atomic one-shot replay rejection; race and capacity behavior; and rejection of
construction, copy, pickle, subclass, reflection, mutation, and id-reuse
attacks. They found no false GREEN/RED, scope drift, compatibility regression,
or artifact inconsistency. The source/test anchors remain exactly those in
TK4-E013. Exact six-path staging and staged delivery gates are authorized.

## TK4 Staged Gate Acceptance — TK4-E015

Only the six authorized TK4 paths were staged, with no unstaged path.
`git diff --cached --check` and exact staged-name inspection passed. The staged
full repository command exited 0 in 15.93 seconds with 1758 passing, 81
deselected, and the same two known passing macOS fork warnings (pytest reported
14.80 seconds). Whole-repository Ruff check, candidate format, compilation,
exact pure package import, cached whitespace, and hash gates all passed.

The staged executable anchors remain exactly those in TK4-E013. The staged
artifact before this evidence append was
`76c325ae204169ccbcdaef725aef2989884afa3c39ea85cdd18998bd37fbef6a`.
The controller must restage only this ledger append, confirm that no executable
anchor changed, and repeat the exact staged full/static/name/diff gates before
the authorized commit. On success, commit exactly
`feat(validation): verify deterministic acceptance criteria` and push
non-force; otherwise do not commit.

## TK4 Closeout and TK5 Resume — TK5-E000

TK4 was committed exactly as
`f4a305c02af81f72d6196c7a872c8c6374d8d7a6`
(`feat(validation): verify deterministic acceptance criteria`). The first
non-force push attempt failed before remote mutation because GitHub port 443
was unreachable; an immediate bounded retry succeeded. The controller then
verified a clean `codex/task-kernel-phase2` branch with HEAD exactly equal to
its upstream at that commit. The exact final staged candidate also repeated
the full 1758-pass, 81-deselected, two-known-warning regression and all static,
name, diff, import, and hash gates required by TK4-E015.

Under TK-R1/TK-A02, TK-A03, and the user's standing instruction to continue
without internal product approvals, the already approved ordered TK5 packet
now begins. No architecture, public MCP, model, environment, CAD, dependency,
or external-path authority is expanded.

## Task Packet TK5-P1 — Immutable Local CAD Revisions

### 1. Anchor and authority

TK-R1/TK-A02 explicitly approved TK5, TK-D05–TK-D12, TK-D16, TK-D18, TK-D24,
the nine-commit sequence, named-file scope, genuine RED, independent review,
exact commit, and non-force push. TK-R2/TK-A03 remains controlling for internal
oracle/review repair. Anchor:
`codex/task-kernel-phase2@f4a305c02af81f72d6196c7a872c8c6374d8d7a6`,
upstream-equal and clean before this append. No duplicate user approval is
required.

### 2. Product outcome and contract boundary

Implement a pure local revision repository that turns controller-owned FCStd
and artifact bytes into immutable project revisions, then atomically advances
a small authenticated HEAD record. A committed revision has an opaque
controller-owned identifier, canonical manifest, exact model/artifact hashes
and sizes, and a journal that distinguishes preparation, HEAD linearization,
and completed bookkeeping. Recovery must report the only state supported by
durable evidence; it never guesses, edits an old revision, or rolls back an
already advanced HEAD.

The store supports an explicitly initialized empty project and import of an
FCStd supplied by a trusted same-process host. It does not open or interpret
CAD geometry, create a Session, export a file, expose arbitrary user/model
paths, acquire a network resource, or invoke MCP/model/FreeCAD. Candidate
staging and transaction names are generated internally. TK6/TK7 remain the
only owners of candidate-session behavior and controlled CAD export.

### 3. Exact allowlist and surface

Only these paths may change:

- `docs/orchestrated/vibecad-task-kernel-phase2.md`
- `src/vibecad/execution/revisions.py`
- `tests/test_revision_store.py`

The direct `vibecad.execution.revisions` module will expose strict immutable
revision, artifact, HEAD, journal, reconciliation, trust, and error values plus
one `LocalRevisionStore`. `src/vibecad/execution/__init__.py` remains unchanged;
there is no public server/MCP tool, registry entry, dependency, compatibility
shim, plugin discovery, or import-time storage mutation. Exact constructor,
mapping, storage-layout, journal, error, and method surfaces are frozen in the
test anchor before the production module exists.

### 4. Test-first execution and gates

1. Run the unchanged full baseline at the clean pushed TK4 anchor with only
   this artifact append present.
2. Resolve the exact value schemas, method state machine, canonical bytes,
   storage layout, durability boundary, and crash-window reconciliation with
   two bounded independent read-only design reviews. The controller writes
   `tests/test_revision_store.py` first.
3. Freeze an independently accepted test candidate and capture one genuine RED
   caused solely by absent `vibecad.execution.revisions`.
4. Implement only `revisions.py`. Focused GREEN covers initialization, trusted
   FCStd import, prepared revision commit, immutable reads, HEAD advancement,
   journal completion, and deterministic reconciliation, including every
   pre-/post-linearization fault window.
5. Run cumulative TK1–TK5 and Phase-1 workflow tests, the full repository suite,
   whole Ruff check, candidate format, pure import, dependency/AST/diff/hash/
   allowlist gates, and two fresh complete read-only reviews.
6. Stage only the three named files, rerun staged gates, commit exactly
   `feat(execution): persist immutable CAD revisions`, push non-force, and
   verify clean HEAD/upstream equality before TK6.

### 5. Required adversarial matrix

Tests must cover malformed, missing, empty, oversized, hash-mismatched,
directory, symlink, non-regular, and unsafe-link model/artifact inputs;
project/revision/transaction/path traversal and Unicode/separator attacks;
private permissions and root/project/revision/HEAD/journal entry replacement;
partial write, flush, file-fsync, replace, directory-fsync, cleanup, and read
faults; duplicate-key, noncanonical, truncated, oversized, and checksum-bad
records; and old-revision byte identity after every failure.

The crash matrix includes candidate staging only; a complete orphan revision
before HEAD; HEAD advanced with a prepared journal; committed journal after
HEAD; corrupt or mismatched HEAD/manifest/content; retry and duplicate
reconciliation; and concurrent or stale transaction observations. Each case
must produce a stable redacted result or error and must never infer commitment
from timestamps, names, or incomplete files.

### 6. Budgets and circuit breakers

Use one genuine missing-module RED and at most two focused GREEN attempts.
Bound manifest/record bytes, JSON depth/nodes/strings, project artifact counts,
individual and aggregate artifact sizes, copied-stream chunks, and diagnostic
text. Reject unknown/untrusted roots, network-filesystem claims, absent atomic
rename/fsync/no-follow capabilities, mutable public state, arbitrary path
selection, or unbounded recovery scans. Stop on an unexpected RED, second
unsuccessful GREEN, ambiguous HEAD linearization, recovery that must guess,
any old-revision mutation, scope drift, or unexplained baseline regression.
Internal oracle/review defects remain recoverable under TK-A03 when recorded
and independently re-reviewed; they require no product decision.

### 7. Roles and delivery

The controller owns artifact/test/source edits, serialized executable
validation, exact staging, commit, and push. Independent reviewers separately
cover schema/compatibility and filesystem/durability/recovery threats, followed
by two fresh final complete reviews. Review workers use static read-only shell
only; they do not run Python, pytest, Ruff, formatters, CAD, network/model
services, or mutate files/Git. No long-running command is relaunched while its
original session is active.

Deliver one independently accepted pushed TK5 commit and a clean recovery
snapshot. Preserve Session creation, receipt consumption, candidate promotion,
geometry observation, export policy, TaskService orchestration, public MCP,
cloud/object storage, Windows durability claims, and network filesystem support
for later packets. Do not begin TK6 until TK5 is clean and upstream-equal.

## TK5 Baseline Evidence — TK5-E001

At clean pushed TK4 anchor
`f4a305c02af81f72d6196c7a872c8c6374d8d7a6`, with only the approved TK5
artifact append in the worktree, `PYTHONPATH=src .venv/bin/pytest -q` exited 0
in 20.56 seconds with 1758 passing, 81 deselected, and the same two known
passing macOS multi-threaded-fork deprecation warnings (pytest reported 18.57
seconds). No TK5 source/test, dependency, environment, CAD, network, model,
staging, or Git mutation occurred.

## TK5 Resolved Revision Contract — TK5-E002

Three bounded static read-only reviews independently covered revision schemas,
filesystem/durability threats, and TK5→TK6/TK8 compatibility. No product-level
blocker exists. The controller adopts the conservative Stage-C-only choices
below under TK-A03 and does not expand format, retention, history, platform, or
storage scope.

The direct module's exact public surface is `RevisionStoreRootTrust`,
`RevisionStoreErrorCode`, `RevisionStoreError`, `RevisionArtifactRef`,
`RevisionRef`, `ProjectHead`, `CommitJournalState`, `CommitJournal`,
`ReconciliationStatus`, `ReconciliationResult`, and `LocalRevisionStore`.
Every value is schema-v1, frozen, slotted, keyword-only, strictly mapped, and
pathless. `RevisionArtifactRef` uses the same id/name/format/SHA-256/size field
vocabulary as TK1's task artifact reference. `RevisionRef` binds project,
revision, base revision, manifest digest, optional fixed FCStd model reference,
and fixed STEP artifact references. `ProjectHead` binds project, generation,
revision, and manifest digest. Journal and reconciliation values expose only
opaque IDs/digests and deterministic state, never host paths or native errors.

An empty project has a real initial `revision_<32hex>` and generation-zero
HEAD, with an immutable manifest whose model is null and artifacts are empty.
This is metadata for an empty CAD base, not a fabricated FCStd, and is required
because the already delivered TaskRun contract has a mandatory canonical base
revision. Trusted FCStd initialization instead creates a generation-zero
initial revision with fixed `model.FCStd`/`fcstd` metadata and no STEP. Every
later sealed candidate must contain a nonempty fixed `model.FCStd` plus exactly
one nonempty fixed `model.step`; only `fcstd` and `step` are admitted.

The exact store lifecycle is:

1. `initialize_empty_project(project_id, lease)` or
   `import_trusted_fcstd(project_id, source, lease)` atomically creates one new
   project at generation zero.
2. `begin_revision(project_id, expected_head, lease)` generates the candidate
   revision and transaction IDs, creates a private candidate, copies any base
   model, and persists a `staging` journal.
3. `candidate_model_path(...)` and `candidate_artifact_path(..., "step", ...)`
   expose only store-owned fixed paths to the trusted same-process CAD ports.
   No model/user string selects either path.
4. `seal_revision(...)` copies those fixed files into a store-owned immutable
   revision, hashes and sizes them, writes and verifies the canonical manifest,
   atomically publishes the complete revision, advances the journal to
   `prepared`, and returns its pathless `RevisionRef`. The old writable paths
   no longer authorize or influence the sealed bytes.
5. `commit_revision(project_id, expected_head, revision_id, lease)` revalidates
   HEAD, journal, manifest, and every byte before atomically replacing HEAD.
   The replacement is the sole logical commit point. It then records
   `committed`; no later cleanup or reporting failure may roll HEAD backward.
6. `rollback_revision(...)` and `reconcile(project_id, lease)` classify clean,
   committed, not-committed, and cleanup-required states from exact evidence.
   `load_head`, `load_revision`, `revision_model_path`, and
   `revision_artifact_path` perform strict immutable readback without scanning.

All project mutations require an exact active matching `ProjectWriteLease`
issued by the exact manager injected into the store. The store never
reacquires that lock: TK8's approved order already holds it and the lease is
intentionally non-reentrant. Atomic immutable reads do not acquire it. A
candidate revision ID is generated before CAD execution and remains the final
revision ID, so TK4's receipt candidate/manifest binding cannot be changed at
commit time.

The single fixed journal states are `staging`, `prepared`, `committed`, and
`not_committed`. A staging/prepared journal with the exact old HEAD is resolved
fail-closed as not committed; recovery never resumes the CAD transaction or
advances HEAD. A prepared/committed journal with the exact new HEAD and valid
content resolves committed. A terminal journal is idempotent until the next
begin consumes it. Any third HEAD, foreign/multiple transaction evidence,
missing or corrupt content, digest mismatch, or inconsistent state raises
`recovery_required`. Candidate-only and complete orphan evidence never become
HEAD by name, generation, timestamp, or scan; uncommitted sealed bytes remain
private and may not mutate an older committed revision.

Canonical JSON is compact, UTF-8, sorted-key, duplicate-key-free, float-free,
and byte-canonical with separate checksum domains for manifest, HEAD, and
journal. Project, revision, transaction, and artifact IDs map through separate
domain-separated SHA-256 path keys. Root/project/revision/candidate directories
are owner-only; stored files are owner-only ordinary single-link files on the
trusted root device. Traversal is dir-fd-relative with no-follow semantics and
identity checks. Fixed caps are 16 KiB HEAD, 32 KiB journal, 256 KiB manifest,
64 JSON depth, 4096 JSON nodes, 4096 UTF-8 bytes per string, 512 MiB per input,
1 GiB per revision, one STEP artifact, and 64 KiB copy chunks. Reconciliation
uses fixed names and never performs an unbounded filesystem scan.

The exact public error codes are `invalid_identifier`, `invalid_input`,
`not_found`, `already_exists`, `conflict`, `corrupt_record`,
`corrupt_content`, `budget_exceeded`, `unsafe_store`, `invalid_lease`,
`io_error`, `durability_uncertain`, `recovery_required`, and
`cleanup_required`. Messages are fixed and redacted. Only the uncertainty
error may carry a strict `head_committed` boolean; it never contains source
path, filename, JSON, errno, or arbitrary exception text.

No `execution.__init__` change, FreeCAD/Session/tool/server/validation/MCP/model
import, network or dynamic callable, TaskRun mutation, ambient `open`,
filesystem discovery walk, dependency, or Windows/network durability claim is
admitted. The reviews agree these choices close TK5 without a user decision.
The controller may now write the complete test candidate before production
exists and request two independent static test-oracle reviews.

## TK5 Frozen Test-Oracle Acceptance — TK5-E003

The controller completed the approved production-before-source oracle loop
under TK-A03. An intermediate 2913-line candidate at
`1cdab4a55cef1a08d1c5718215753fa5c5baca01fb065748eed5a3bf37192ac1`
received two zero-finding reviews, while the independent durability review
correctly rejected it with two P1 and four P2 findings. Those findings exposed
missing direct proof for staging-journal and imported-FCStd durability, weak
cross-phase event windows, incomplete exact JSON-resource edges, exceptional
worker cleanup risk, and a nonbehavioral exact-import assertion. No production
source or executable test was run at that rejected anchor.

The superseding frozen test is exactly 3141 lines with SHA-256
`057188533f2d41950b82bd4dd55781b96d05a697602d29799a9b0b06575db5fb`.
It adds staging journal file/containing-directory fsync ordering and targeted
faults, imported-model prepublication fsync ordering and failure cleanup,
strict prepared-journal/HEAD/committed-journal local durability windows, exact
64/65-depth, 4096/4097-node, and 4096/4097-UTF-8-byte parser edges, bounded
daemon worker shutdown, and an allowed dependency-boundary AST check without
irrelevant exact optional imports. It preserves every E002 public contract and
the source remains absent.

At this exact anchor, Ruff check exited 0, Ruff format check exited 0, Python
compilation exited 0, `git diff --check` exited 0, and the explicit
source-absence gate exited 0. Two fresh independent full-file static reviews
then each returned ACCEPT with P0/P1/P2 exactly `0/0/0`: one covered the whole
contract/collection oracle and one re-audited every filesystem durability,
recovery, budget, and concurrency finding. Both confirmed the exact hashes,
line count, absent production module, and unchanged workspace. This authorizes
one focused RED. It is accepted only if collection fails solely with
`ModuleNotFoundError: No module named 'vibecad.execution.revisions'`; every
other outcome is an unexpected gate red and freezes implementation.

## TK5 Genuine Focused RED — TK5-E004

At artifact revision
`6ed6bcb610aaf69641a4110909ec24205b74bd2fb462a1e003b7460e8103fdd8`
and unchanged frozen test
`057188533f2d41950b82bd4dd55781b96d05a697602d29799a9b0b06575db5fb`,
the controller reconfirmed that `src/vibecad/execution/revisions.py` did not
exist and ran exactly one focused command:

    PYTHONPATH=src .venv/bin/pytest -q tests/test_revision_store.py

It exited 2 in 1.3 wall seconds after pytest reported 0.50 seconds. Collection
produced exactly one error at test line 16 and no executed test: Python raised
`ModuleNotFoundError: No module named 'vibecad.execution.revisions'`. This is
the sole predicted missing-production-module RED authorized by TK5-E003. No
syntax, fixture, dependency, environment, or wrong-path failure appeared. The
RED is accepted and consumed; do not rerun a missing-module RED. The controller
may now create only `src/vibecad/execution/revisions.py` and attempt the first
focused GREEN against the unchanged frozen oracle.

## TK5 Post-RED Oracle Correction Policy — TK5-E005

A third static reviewer returned after TK5-E004 and demonstrated that the AST
dependency gate could be bypassed by importing or aliasing ambient filesystem
operations. Because production still did not exist, the controller froze
source creation and repaired the oracle under TK-A03. Several preserved review
rounds showed that an attempted general Path data-flow inference created both
new bypasses and false positives across Python scopes and helper returns. Those
rejected candidates were never executed and do not supersede E004.

The final correction deliberately uses a smaller auditable source policy:
inside this security-critical revision module, filesystem operations are made
only through the closed `os` call/attribute allowlists. Every non-`os`
attribute whose name denotes an ambient filesystem read, mutation, discovery,
ownership, link, or metadata operation is forbidden at attribute-load time,
including aliases; dynamic calls and ambient `open` remain forbidden. This
also intentionally forbids unrelated same-named conveniences such as
`dataclasses.replace` or `str.replace` in this one module. Neither is needed by
the approved implementation, and the restriction avoids claiming an unsound
Python type/data-flow proof. Pure Path construction, `absolute`, parts,
is-absolute checks, `/` composition, and returned Path values remain admitted.
Reflective namespace/attribute access, code/class internals, and call targets
formed by a subscript or another call are likewise excluded, closing alias
recovery through `__dict__`, `__getattribute__`, or namespace reflection.

This is an internal implementation constraint, not a product/API or dependency
change. It narrows the already approved E002 no-ambient-path boundary and
requires no new user decision. The superseding test must pass static checks,
receive two fresh zero-finding reviews against this explicit policy, and
produce one replacement source-absent RED before any production file is
created. The earlier E004 RED remains valid historical evidence but is not the
implementation anchor after this append-only correction.

## TK5 Namespace-Relay Review Rejection — TK5-E006

At artifact revision
`fdb1b0622d29c1819537cc4a48e31d68c9b239f5d19f3d7e0d4f80f7ac031cfe`
and 3207-line test candidate
`98d0917fd216dc63abacffa11d7c537ce96f58e70883751d2d21aff6f8a6e2a6`,
the two fresh independent reviews returned one ACCEPT at P0/P1/P2 `0/0/0`
and one REJECT at `0/1/0`. The rejecting review demonstrated that an allowed
non-`os` dependency could still relay filesystem authority: for example, the
candidate admitted `from vibecad.workflow.lease import _open_root`, and a
module import could expose that dependency's private `os` binding. The global
forbidden-name sets did not constitute a positive semantic import boundary.

The production module remains absent and no replacement RED was run at this
rejected anchor. Under the already approved TK-A03 correction authority, the
controller will replace the negative non-`os` import filter with a closed
per-module symbol map. Both direct `from` imports and aliased module attribute
access must resolve only to approved symbols; dotted module imports without an
explicit alias are rejected. This is a test-oracle hardening correction within
E005, not a product, public API, dependency, storage, or platform decision.
Source creation stays frozen until the superseding candidate passes all static
gates and receives two fresh zero-finding reviews.

## TK5 Import-Binding Shadow Review Rejection — TK5-E007

At artifact revision
`e639038942e6082c6032fd3d5fb17f3bdd60bff2605d311aa1308fba47571217`
and 3209-line test candidate
`c098d2e2d291f2798cce0168486c24984d2d56260073f2212e1304600736837e`,
the next fresh reviews again returned one ACCEPT at P0/P1/P2 `0/0/0` and one
REJECT at `0/1/0`. The positive module map closed the E006 relay, but the
rejecting review showed that a module alias could be shadowed by a binding
which is not represented by an `ast.Name`: a function parameter named like an
`os` alias could receive a `Path` and then call its `open` method while the
gate continued to classify the name as the original `os` module.

The production module remains absent and no executable gate was run at this
anchor. The TK-A03 correction is extended conservatively without lexical
data-flow inference: every approved imported binding must be unique across the
entire module and may not be rebound. Import aliases, assignment targets,
arguments, function/class definitions, exception targets, pattern captures,
and type-parameter bindings are counted. This deliberately forbids harmless
shadowing inside this one module in exchange for a small auditable policy. The
superseding candidate still requires static gates and two new independent
zero-finding reviews before the replacement missing-module RED.

## TK5 Capability-Set Relay Review Rejection — TK5-E008

At artifact revision
`eaa635e2df0b95f3cc2b68a4044783fd51c94df2df622d997f97d378ce57da7e`
and 3235-line test candidate
`cb9c6353c63e48824f41a6070340400f5bc63a24147024ee668c032c8df4ea51`,
the next reviews returned one ACCEPT at P0/P1/P2 `0/0/0` and one REJECT at
`0/1/0`. The E007 shadow fix was accepted, but the rejecting review showed
that the admitted `os.supports_dir_fd` and `os.supports_follow_symlinks` sets
expose unapproved native functions which could be selected and called through
an ordinary local name. The controller's same-boundary audit also confirmed
that approved built-in `os` functions expose their underlying module through
the reflective `__self__` attribute.

The production module remains absent and no replacement RED was run. Neither
capability set is required by the frozen contract, so TK-A03 removes both from
the allowed source surface instead of attempting contextual iteration
analysis. All double-underscore attribute access is also rejected in the
production module, closing the function-module reflection route without
affecting private lease identity fields or any approved public contract. The
next candidate remains subject to static gates and two fresh independent
zero-finding reviews.

## TK5 Dependency-Method and Dynamic-Call Review Rejection — TK5-E009

At artifact revision
`b24fe08bec0470bd12a52af00882be5ccf30867c212a7cbbde5cf3330caabb15`
and 3234-line test candidate
`ab4bd431a71447f5bca777fa2a0db69357618782ca0a491ec38547319f5ea0bb`,
one fresh full review returned ACCEPT at P0/P1/P2 `0/0/0`, while two
independent adversarial reviews each returned REJECT at `0/1/0`. The first
rejection showed that the required lease manager or lease instances could
relay lock-filesystem authority through `acquire`, `acquire_project_write`,
generic `release`, the manager adapter, or stored lock descriptors. The second
showed the more general cause: any non-module attribute call such as
`source.read()` or `self._callback()` was still admitted. Merely extending an
attribute blacklist would not prove the E005 no-dynamic-call policy.

The production module remains absent and no replacement RED was run. Under
TK-A03 the source policy now adopts closed semantic call sets. Name calls are
limited to explicitly admitted built-ins, immutable approved imports, and
unique top-level callables defined by this module. Non-module attribute calls
are limited to the small value-operation set required by the frozen
implementation; module calls are limited separately per approved module.
Every approved built-in and top-level callable name is non-shadowable. Context
manager syntax is excluded so a supplied lease cannot implicitly release
itself. Lease manager adapter/root/descriptor fields and all acquisition or
release methods are explicitly outside the source surface; only the identity
fields required by the frozen exact-lease proof remain available. This is a
stricter implementation constraint, not a public API or product decision.

## TK5 Implicit-Call, Provenance, and Process Review Rejection — TK5-E010

At artifact revision
`bded7d61a55322224e71d2480759e57bb81b319ed4a284472968e99df72d1ebc`
and 3306-line test candidate
`e62532e3ac95e54333c7bd52c2d33d225607d4478416e1263714804dda0bcfc4`,
the three fresh independent reviews all rejected the candidate. Two reported
P0/P1/P2 `0/1/0`; the compatibility review reported `0/1/1`. The findings
were complementary: bare decorators still performed unchecked implicit
calls; the admitted hash/Path method names and broad conversion built-ins
still lacked receiver/argument provenance; and a store inherited by a forked
process could not distinguish its otherwise exact inherited lease because
`getpid` was absent. The P2 also correctly noted that E005 still described
Path `absolute`/`is_absolute` calls which the narrowing policy no longer
needed or consistently admitted.

The production module remains absent and no replacement RED was run. TK-A03
adopts the following final conservative correction. Only the exact frozen,
slotted, keyword-only `dataclass` decorator call and exact `staticmethod` on
the five public `from_mapping` parsers are admitted; class bases and keywords
are closed. Hash `update` and `hexdigest` receivers must be a unique local
state created directly by an approved SHA-256 constructor, except that a
one-shot approved SHA-256 call may receive `hexdigest` directly. General
Path method calls are removed: an exact constructed Path is checked through
its pure `parts` value, and E005's mention of `absolute` and `is_absolute` is
superseded accordingly. Conversion built-ins are reduced and their argument
shapes are fixed, including only two-argument UTF-8 `bytes`/`str` conversion.

`os.getpid` is added to the closed call set. The store captures its creator
process and every mutation rejects a different current process before storage
access. A deterministic process-context oracle will change the observed PID
while retaining the exact active lease and prove both `invalid_lease` and
unchanged durable bytes. This is the already implied exact-active-lease
condition from TK2/TK5, not a new product behavior. These corrections remain
subject to all static gates and two fresh zero-finding reviews before the
replacement source-absent RED.

## TK5 Hook, Creator-PID, and Protocol Review Rejection — TK5-E011

At artifact revision
`a3ce78c15a15f8b1b532b2f79b671a191d1c20badf9dafa1b1ba59453c2f2c48`
and 3439-line test candidate
`232bcbd50ca6cad22c569656c071e3e4e811041c5211bb4cd711b84079dc9e11`,
all three fresh reviews rejected the candidate. The full and blueprint reviews
reported P0/P1/P2 `0/1/0`; compatibility reported `0/1/1`. Unshaped JSON
keywords could still invoke `object_hook`, `parse_*`, or `default` callbacks.
The PID test changed process identity only after an earlier mutation, so a
module-import capture or first-mutation lazy capture could pass incorrectly;
it also did not freeze module-qualified `os.getpid`, making a direct imported
binding invisible to its monkeypatch. Finally, admitted `len` and generator
`tuple` calls still invoked arbitrary object protocols without provenance.

The production module remains absent and no replacement RED was run. TK-A03
closes these paths without general data-flow inference. Canonical
`json.dumps` receives exactly one value plus the four fixed canonicalization
options. `json.loads` receives exactly one string plus one fixed
`object_pairs_hook`, which must name the unique top-level duplicate-key parser;
all other JSON hooks and keyword expansion are excluded. Other approved module
calls receive closed arity and keyword shapes where they can expose hooks or
protocol conversion. Direct `from os import getpid` is rejected so the single
module-qualified PID seam remains observable.

A new fresh-store oracle will construct the manager, store, and exact live
lease under synthetic PID A before any mutation, change to PID B, and require
the inherited store to reject before `os.open`. It then constructs a separate
manager and store under PID B and requires normal initialization, rejecting
both module-import and first-use capture errors. `len` and callable `tuple`
are removed entirely; bounded parsing uses explicit counters and the frozen
zero-or-one artifact model uses tuple literals. These remain internal source
constraints and require two fresh zero-finding reviews before RED.

## TK5 Import-Time, Expansion, and Handler Review Rejection — TK5-E012

At artifact revision
`0344186a358e48dfac1eb0cde6dde05f015836c8e5a3daf4b535eb1f985f31d6`
and 3558-line test candidate
`2d57fa36af1a43f78eac31245bb9f1ac83ac9f69a8348a32da1dfee0d0a96425`,
two fresh independent reviews returned ACCEPT at P0/P1/P2 `0/0/0`; the final
compatibility review returned REJECT at `0/3/0`. Its three demonstrated paths
remain authoritative despite the two accepts. Approved `os` calls could still
execute in a module or class body during import. Starred arguments, container
unpack, and `**` expansion could still invoke arbitrary iteration or mapping
protocols. A broad exception tuple could alias `BaseException` and bypass the
direct handler-name check.

The production module remains absent and no replacement RED was run. TK-A03
requires every explicit call except the exact dataclass decorator to be inside
the executable body of a top-level function or class method; calls in module
or class bodies, defaults, annotations, decorators, or nested definitions are
rejected. Definitions themselves are module-level or methods only. Lambda,
async/generator/comprehension call surfaces, starred nodes, call `**`, mapping
unpack, and variadic definitions are excluded because none is needed by the
frozen implementation.

`Exception` and `BaseException` are forbidden everywhere. Exception handlers
are positively limited to the non-shadowable built-in `OSError` and
`UnicodeDecodeError`, plus the exact approved JSON decode error, including
closed tuples of only those types. Explicit counters, ordinary loops over
exact type-checked internal values, fixed tuple literals, and `try/finally`
remain available. This correction preserves the public contract and again
requires static gates plus two fresh zero-finding reviews before RED.

## TK5 Annotation, Enum, Destructure, and PathLike Review Rejection — TK5-E013

At artifact revision
`2ca8912a705a6eb35ad1d497999f4b773b8e0bea6627f3b2194bf10b0bc374f2`
and 3620-line test candidate
`b2481c20ed27a47f9bb0a56e40e953ab516b67aba220ad8cbf50dd961bdc90cb`,
the blueprint review returned ACCEPT at P0/P1/P2 `0/0/0`; the full review
returned REJECT at `0/1/0` and compatibility returned REJECT at `0/3/0`.
Unforced annotations could invoke a class subscription protocol during import.
An admitted StrEnum lifecycle method could be invoked by EnumMeta at class
creation. Ordinary sequence destructuring and structural matching still
invoked iteration/mapping protocols. Finally, `Path(value)` had no oracle
proving an arbitrary caller PathLike was rejected before `__fspath__` ran.

The production module remains absent and no replacement RED was run. TK-A03
now requires exactly one unaliased absolute
`from __future__ import annotations` directive and rejects any alternate
future import. Module and class bodies follow a closed grammar: imports,
literal constants, the exact public classes, top-level helpers, and the exact
decorators only. There are no internal classes. StrEnum classes expose only
their frozen member constants and no source methods. The five value classes,
the public error, and `LocalRevisionStore` each expose only their exact required
source methods; the only admitted source dunders are the required `__init__`
and dataclass `__post_init__` methods. Function defaults are literals.

Structural matching, tuple/list store destructuring, deletion, and non-name
loop targets are excluded. Ordinary loops over values already proven to be
exact built-in containers remain available. New hostile-PathLike oracles will
pass a protocol object as the store root and trusted-import source, require the
closed `unsafe_store`/`invalid_input` error respectively, and prove
`__fspath__` was never invoked. Production must exact-type-check accepted
`str` or concrete local Path values before calling the approved Path
constructor. These restrictions preserve the approved product behavior and
again require static gates plus two fresh zero-finding reviews.

## TK5 Iteration Provenance and Exact Schema Review Rejection — TK5-E014

At artifact revision
`92764573c00c66d2e04b1aabac36475b1703c4ff442fe4bbf5b92372739036c1`
and 3750-line test candidate
`8e5172536bf0f5cd663d02c511f8fed2d3c9c0f1de2b1b46e534e15dc856e489`,
the implementation-feasibility review returned ACCEPT at P0/P1/P2 `0/0/0`.
The full review returned REJECT at `0/1/0`, and compatibility returned REJECT
at `0/0/2`. The accepting review does not override either concrete finding.
An ordinary `for` iterator was only required to have a single-name target; it
was not required to be an exact built-in value before iteration, so an
untrusted public parameter could still invoke `__iter__`. The hostile input
only covered `__fspath__`. In addition, the E013 exact `str` root/source
acceptance had no positive oracle, while the five value-class field schemas
and three non-error enum member sets were not closed against additions.

The production module remains absent and no replacement RED was run. TK-A03
now requires every admitted ordinary loop iterator to be a single-binding
name dominated by an exact built-in type guard whose rejecting branch exits;
the iterator cannot be rebound between that guard and the loop. This retains
auditable loops over exact `dict`, `list`, `bytes`, `str`, or literal-tuple
types without admitting arbitrary iteration protocols. The hostile root and
trusted-source fixture covers filesystem coercion, iteration, equality,
indexing, containment, truth, length, hashing, formatting, string/bytes, and
integer conversion protocols and proves none runs before the closed error.

Positive lifecycle oracles accept exact `str` roots and trusted FCStd source
paths as well as concrete local Path values. Runtime reflection and the source
grammar both freeze the exact five dataclass field sequences and constructor
signatures, including the sole schema-version default. They also freeze the
exact member names and values for root trust, journal state, reconciliation
status, and error code, and strict mapping tests reject unknown durable enum
values. These are internal oracle corrections, preserve the approved product
surface, and require static gates plus two fresh zero-finding reviews before
the replacement RED.

## TK5 Corrected Frozen Test-Oracle Acceptance — TK5-E015

The corrected artifact at
`fe7937a4612eabca59afcf693fcb59e420b72d3eb0d9b48b8335fb2a68d73700`
and 4025-line test oracle at
`b5ab7452587be760f251f25c445077575aed5d50b2c610e6fd4f483dba66f963`
completed every pre-execution gate with the production module absent. Ruff
check exited 0, Ruff format check exited 0, Python compilation exited 0,
`git diff --check` exited 0, and the explicit source-absence gate exited 0.

Three fresh independent full-file reviews then returned unconditional ACCEPT
with P0/P1/P2 exactly `0/0/0`. F10 re-audited implicit protocol, import-time,
dynamic call, loop-provenance, and filesystem-relay boundaries. B7 derived a
complete implementation under the exact guard, grammar, exception, and
iteration restrictions and found no false RED. C10 rechecked every public
schema, enum, exact `str`/Path input, lifecycle, durability, recovery, budget,
PID, and lease requirement; its preliminary concern about shadowing `dict`
and `list` was withdrawn after confirming both names and all bindings are
protected by the current oracle.

The reviews confirmed HEAD
`f4a305c02af81f72d6196c7a872c8c6374d8d7a6`, the exact hashes and line counts,
the absent production source, and an unchanged workspace. This freezes the
corrected oracle and authorizes exactly one replacement focused RED. It is
accepted only if collection executes no tests and fails solely because
`vibecad.execution.revisions` does not exist. Any other result freezes source
creation for investigation.

## TK5 Corrected Focused RED — TK5-E016

At artifact revision
`dd1a61ba10dd0f42516d3de0d2a1063322bceb6f6f24206ba0d9a59d8695b26e`
and unchanged 4025-line frozen test
`b5ab7452587be760f251f25c445077575aed5d50b2c610e6fd4f483dba66f963`,
the controller reconfirmed the production module was absent and ran exactly
one replacement command:

    PYTHONPATH=src .venv/bin/pytest -q tests/test_revision_store.py

It exited 2 in 1.5 wall seconds after pytest reported 0.61 seconds. Collection
executed no test and produced exactly one error at test line 16:
`ModuleNotFoundError: No module named 'vibecad.execution.revisions'`. No syntax,
fixture, dependency, environment, or alternate-path failure appeared. This is
the sole predicted missing-production-module RED authorized by TK5-E015. The
RED is accepted and consumed; it must not be rerun while the source remains
absent. Production implementation may now begin against this exact frozen
oracle.

## TK5 Python 3.13 Socket-Fixture Compatibility Correction — TK5-E017

With the frozen 4025-line oracle
`b5ab7452587be760f251f25c445077575aed5d50b2c610e6fd4f483dba66f963`
and initial production source corrected to
`718ed002342e0a515beed8bfbaa7dc8e8e30992342e8c2c9b3d4a1f3c3f930de`,
the first controller GREEN attempt passed 35 tests before exposing an
over-strict artifact-name validator. Production was correctly narrowed so an
individual safe basename is admitted while `RevisionRef` still enforces the
fixed model/STEP roles; the exact failed test then passed.

The next `-x` run passed 83 tests and stopped in test fixture setup before any
revision-store call. On the installed global Python 3.13.14 runtime,
`socket.socket.bind` rejected the supplied concrete `PosixPath` with
`TypeError: a bytes-like object is required, not 'PosixPath'`. The socket entry
exists only to construct an unsafe Unix-socket source for the import rejection
oracle. Converting that already trusted pytest temporary Path to exact `str`
at the bind call preserves every product assertion and still passes the same
Path object to `import_trusted_fcstd`.

TK-A03 authorizes the literal fixture-only correction from
`listener.bind(source)` to `listener.bind(str(source))`. Production must not
import, patch, or otherwise alter socket behavior. This compatibility edit
does not change the schema, lifecycle, security boundary, expected error, or
the consumed RED; it requires static gates and two fresh read-only confirmations
before final TK5 acceptance, but it does not require or authorize another
missing-module RED now that production exists.

## TK5 macOS AF_UNIX Fixture-Length Correction — TK5-E018

The literal E017 conversion correctly reached `socket.bind`, which then
failed before the store call with `OSError: AF_UNIX path too long`. macOS caps
the Unix-domain socket address well below the full pytest temporary path used
by this parametrized test. The fixture now places only the socket node at the
short name `s` in the same pytest-owned run directory and continues to pass
that concrete Path unchanged to `import_trusted_fcstd`. The unsafe source type,
expected `invalid_input` result, cleanup ownership, and all production behavior
are unchanged.

Materializing the previously absent first-party production module also caused
Ruff's import classifier to require the existing `vibecad.execution` import
before its `revisions` submodule. That reorder is mechanical and has no runtime
or oracle effect. E017 and E018 together remain a fixture-only Python/macOS
compatibility correction; they do not reopen the product contract or authorize
another RED.

## TK5 Focused GREEN — TK5-E019

At artifact revision
`73b03b76bfe7bfc82fea1b59e6bb2c093a24413d984185ef7a6b55a6b88202ed`,
the corrected 4026-line test oracle was
`6fbdc31a1eec1330b7013c7898d536b4c8e2652d20bb3eb08629934a38b30da7`
and the complete 3060-line production module was
`718ed002342e0a515beed8bfbaa7dc8e8e30992342e8c2c9b3d4a1f3c3f930de`.
Ruff check, Ruff format check, Python compilation, and `git diff --check` all
exited 0 against source and test.

The controller then ran:

    PYTHONPATH=src .venv/bin/pytest -q tests/test_revision_store.py -x

It exited 0 in 3.1 wall seconds after pytest reported 2.26 seconds, with all
176 parametrized tests passing and no skip, warning, error, or failure. This
covers the exact public values, strict mappings, trusted root and FCStd import,
immutable begin/seal/commit/readback, rollback and reconciliation, process and
lease binding, dirfd/no-follow/link/mode defenses, canonical JSON and budgets,
partial I/O, every pre/post-HEAD durability fault, concurrency, and the source
AST boundary.

The focused GREEN freezes the current implementation and corrected fixture
for independent source review. No product scope, dependency, FreeCAD, MCP,
network, model, Git, or environment mutation occurred. Final acceptance still
requires two fresh zero-finding reviews plus cumulative and full regression
gates.

## TK5 Post-GREEN Source Review Rejection — TK5-E020

At artifact/test/source anchors
`81367a2c9b3d1562c5ae499b1ed3d9c1fc7bdbeed028e6e51f7a4e8f4a00e7a7`,
`6fbdc31a1eec1330b7013c7898d536b4c8e2652d20bb3eb08629934a38b30da7`,
and `718ed002342e0a515beed8bfbaa7dc8e8e30992342e8c2c9b3d4a1f3c3f930de`,
the controller full suite passed 1934 tests with 81 intentional slow
deselections and the two known macOS fork deprecation warnings. Repository
Ruff check, relevant-file format check, Python compilation, and diff check also
passed. A repository-wide format check remained a non-gate because 72
pre-existing unrelated files are not Ruff-formatted; none was changed.

The subsequent independent filesystem review returned REJECT at P0/P1/P2
`0/0/1`. The independent contract review returned REJECT at `0/2/0`. The
writer's non-independent self-review returned REJECT at `0/4/1`. Their concrete
findings supersede the GREEN acceptance despite the successful runtime suite:

- root and external-source component traversal can lose a newly opened FD if
  closing the previous component raises;
- several cleanup paths short-circuit or ignore close, unlink, rmdir, and
  candidates-directory fsync failures, including successful seal return;
- `_entry_stat` collapses every stat error into absence, so journal I/O failure
  can be reported CLEAN or overwritten by begin;
- strict pre-stat/open inode equality on atomically replaced immutable records
  can reject a legitimate complete old-or-new HEAD under concurrent reads;
- bounded content readback repeatedly concatenates and then re-hashes up to
  512 MiB, causing avoidable quadratic copying and high peak memory;
- revision lineage permits self-base, commit/reconcile do not bind the sealed
  base to the expected HEAD, and a public COMMITTED result need not advance
  generation by exactly one.

No P0, public API expansion, dependency, environment, FreeCAD, MCP, model, or
network issue exists. TK-A03 freezes source edits until targeted regression
oracles reproduce the defects. Fixes must make FD ownership explicit without
short-circuit cleanup, distinguish ENOENT from other stat errors, retry an
atomic record open against a stable safe inode, validate and stream content in
bounded chunks without whole-file materialization for integrity checks, and
enforce exact base/generation lineage at value, record, commit, and reconcile
boundaries. After targeted RED/GREEN, two fresh independent zero-finding source
reviews and the focused/full gates are required again.

## TK5 Post-Review Targeted RED — TK5-E021

At artifact revision
`0a5bb447abe4fc624f97b06305e4fd6e2001e87c18e98577928b8e5c70594bbe`,
the 4532-line corrected test was
`73aab398f58ae4fdf8f3ea87e4b1b8efe29f603913e8d381e34e16e8224e67fa`
and the unchanged pre-fix source remained
`718ed002342e0a515beed8bfbaa7dc8e8e30992342e8c2c9b3d4a1f3c3f930de`.
Ruff check, Ruff format check, Python compilation, and diff check passed for
the expanded oracle before execution.

The controller ran the exact new-test selection for self lineage, committed
generation, streaming model access, wrong-base commit/reconcile/begin,
component-close ownership, non-short-circuit close, candidate authority close,
seal cleanup, journal-stat recovery, and atomic HEAD inode replacement. It
exited 1 after pytest reported 3.13 seconds: all 21 selected regression cases
failed and the 176 prior cases were deselected. Every review finding therefore
has a non-vacuous RED: no expected exception was raised for lineage, cleanup,
or journal evidence; content still reached whole-file record reads; tracked
directory FDs remained unclosed; source/parent and candidate closes were
skipped; and atomic HEAD replacement raised `unsafe_store`.

The targeted RED is accepted and consumed. The 4532-line oracle is frozen and
only `src/vibecad/execution/revisions.py` may now change until all 21 cases and
the prior 176-case focused suite are GREEN.

## TK5 Targeted-Oracle Cross-Review Correction — TK5-E022

After E021, cross-review accepted all controller-authored lineage and streaming
cases at P0/P1/P2 `0/0/0`, but rejected four details in the FD/stat regression
packet. The source double-close tracker matched a basename+dirfd open even
though the frozen security contract opens the source exactly once by absolute
Path. The atomic HEAD case did not count the required retry. The external
traversal close fault expected `not_found` instead of `io_error`, and the
journal-stat case did not prove its injection fired.

The corrected 4540-line oracle at
`c9b19799ee7cea2ed58e96c2953d5a2407e507983d0e0669ffafa69310eb966e`
tracks the existing absolute source open with `dir_fd=None` and retains the
already traversed parent FD; it does not authorize a second source open. It
requires exactly two HEAD opens after deterministic inode replacement, maps an
existing-source traversal close failure to `io_error`, and asserts the journal
stat injection flag. Ruff format/check, Python compilation, and diff check all
pass. E021 remains authoritative for the other non-vacuous REDs; the corrected
double-close case is additionally grounded by F11's independent static defect
trace. No public or product behavior changed.

## TK5 First Corrective GREEN Attempt — TK5-E023

At artifact/test/source anchors
`fea40850d319c2368c2c010b25f5a7a9f47dd2f73893003879959a965fa29f6f`,
`c9b19799ee7cea2ed58e96c2953d5a2407e507983d0e0669ffafa69310eb966e`,
and `9fe3e38816103a59749a5f4178153449a78bf477ab282ac3d02a66c5c3b59bf0`,
the first post-review corrective selection exited 1 after pytest reported 1.58
seconds, with 18 passed, 3 failed, and 176 deselected. All lineage, streaming,
FD-ownership, atomic-HEAD, and four of five seal-cleanup cases were GREEN.

The remaining seal-stat case did not inject after the streaming refactor
reduced the number of model stat calls. Its fixture must bind the injection to
successful PREPARED-journal publication instead of a call count. The two
journal-stat cases exposed one implementation defect: an `OSError` instance
whose native errno is unavailable is currently confused with a successful
stat returning `None`. The correction records stat success explicitly, treats
only errno 2 as absence, and maps every other stat failure to `io_error`; the
existing public reconcile/begin boundary then maps damaged recovery evidence
to `recovery_required`. Neither correction changes the approved product
surface. Static gates and the exact targeted selection are required before
the focused suite resumes.

## TK5 Corrective GREEN and Regression Gates — TK5-E024

After E023, the corrected 4547-line oracle was
`484508e4a3441f9df4d3ae05786fb364dd586f1881cd251d1f114c4ccb7f5d30`
and the 3262-line production source was
`a3389920188760f000bd3b4ee5713b7e4102da7dbca08f822b2135f99104c949`.
Relevant-file Ruff check and format check, Python compilation, and repository
diff check all exited 0. The exact 21-case corrective selection then exited 0
after pytest reported 0.79 seconds, with 21 passed and the prior 176 cases
deselected.

The complete revision-store module subsequently exited 0 after pytest
reported 2.09 seconds, with all 197 cases passing. The repository-wide suite
exited 0 after pytest reported 17.77 seconds, with 1955 passed, 81 intentional
slow cases deselected, and only the two known macOS multithreaded-`fork()`
deprecation warnings in `tests/test_workflow_lease.py`. Repository-wide Ruff
check also exited 0.

These gates cover both the original TK5 contract and every E020 corrective
oracle. The implementation is now a final-review candidate; it is not accepted
until two fresh independent full-file reviews return unconditional ACCEPT with
P0/P1/P2 exactly `0/0/0` at these source and test anchors.

## TK5 Final-Review FD Reuse Rejection — TK5-E025

The first final independent source review at E024 anchors returned REJECT with
P0/P1/P2 `0/1/0`. `_close_owned_fd` retried `os.close` on the same numeric FD
after an error. A close error does not portably prove the descriptor remains
open; the kernel may already have released it, allowing another thread to
reuse the number before the retry. Retrying can therefore close an unrelated
concurrent operation's descriptor.

The correction attempts close exactly once for every owned FD and fails closed
when that attempt reports uncertainty. The two traversal fault oracles now
delegate the first close and then report an error, assert that all owned FDs
received a close attempt, and require exactly one attempt per descriptor. This
models the portable ambiguous-close boundary and rejects the former numeric-FD
retry. The review found the other E020 closure areas sound. Static gates,
targeted/focused/full regression, and two fresh independent zero-finding
reviews are required again before acceptance.

## TK5 Concurrent Atomic-HEAD Gate Rejection — TK5-E026

At artifact/test/source anchors
`a235174ab3179f73de112961c3746aa0aec7aef6c4a466e202f616c971ea9c2b`,
`c198c3b32ced434e51ad08e1d6721764e49e9b17e7d23fa267e0c7150c091c92`,
and `9073411fc073328bb15f2cbc725500406f4f0fa69015327059490a2a1fffc85f`,
both fresh final reviews returned ACCEPT with P0/P1/P2 `0/0/0`, but the
subsequent 197-case controller gate stopped after 189 passes when the
concurrent atomic-reader oracle timed out. An isolated repetition reproduced
the timeout; a bounded multi-iteration diagnostic then captured the reader
exiting with `unsafe_store` during the HEAD open.

The exact race is replacement after opening the old HEAD but before its
`fstat`. The opened old inode can legitimately have link count zero after the
atomic replacement removes its directory entry. Treating that complete old
descriptor as an unsafe immutable record makes an allowed old-or-new reader
fail. A deterministic oracle now performs that replacement at the first HEAD
FD stat and requires a successful old-or-new load without constraining whether
an equivalent safe implementation consumes the complete old FD or closes and
retries. Production remains frozen until this oracle passes static review and
fails for the predicted `unsafe_store` reason. The correction may relax only
the transient unlinked-FD case for explicitly replaceable HEAD/journal records;
immutable revision records remain strict.

## TK5 Deterministic Post-Open HEAD RED — TK5-E027

At artifact/test/source anchors
`52fcd68b254c5b772b4842d8a0357b0fdc0ea81317f07f1b0ef6a45715c993f5`,
`a3eaeb0bd087bfff1977dec744700b2ebc4b7ca3826d679f834d881068b361d5`,
and `9073411fc073328bb15f2cbc725500406f4f0fa69015327059490a2a1fffc85f`,
two independent reviews accepted the deterministic oracle with P0/P1/P2
`0/0/0`. They confirmed exact post-open/pre-fstat timing, non-vacuous injection,
no FD-reuse false hit, and compatibility with both consuming the complete old
inode and retrying the canonical new inode.

The controller then ran only
`test_head_replaced_after_open_is_not_rejected_as_unsafe`. It exited 1 after
pytest reported 0.67 seconds and failed exactly at `store.load_head` with
`RevisionStoreErrorCode.UNSAFE_STORE`; no alternate failure occurred. The RED
is accepted and consumed. Production may now change only inside the replaceable
record open path: a trusted regular owner-mode-device FD whose link count became
zero after open must be closed once and retried within the existing three-attempt
budget. No unlinked FD may be read, returned, or accepted for immutable revision
content.

## TK5 Concurrent Post-Stat HEAD Rejection — TK5-E028

The E027 correction made the deterministic post-open/pre-fstat oracle GREEN,
along with the existing pre-open replacement and concurrent-reader cases. A
100-iteration real concurrency diagnostic nevertheless stopped at iteration
16 when a reader returned `CORRUPT_RECORD`. Instrumentation localized this
second race to `_read_bounded_file`: HEAD opened and passed its initial fstat,
then atomic replacement unlinked the old inode while its complete bytes were
being read. The final fstat legitimately observed the unlink-induced ctime
change and the implementation misclassified it as content corruption.

A second deterministic oracle now replaces the canonical HEAD immediately
after the first native fstat returns but before the implementation consumes
that result. It requires a successful old-or-new read without constraining
whether an equivalent implementation accepts the complete old inode or
restarts against the new pathname. It is scoped exactly to HEAD and does not
relax immutable revision content. The partial E027 source is frozen until this
oracle passes static review and fails solely with the predicted
`CORRUPT_RECORD`; any production correction must still reject changed size,
mtime, type, owner, mode, device, or a linked inode whose ctime changed.

## TK5 Deterministic Post-Stat HEAD RED — TK5-E029

At artifact/test/source anchors
`69d6a325def6ac01674e85f138c9924818ede95c56e17fe83ad1213bce85eea6`,
`e3d64fa5bbc7cb984b53f5c3ca3d3f31af71202c387dad735706f44f2a03a96e`,
and `ee9d3e894037e6aaf383b3cd7ac5442b69ad7bce2fe7e942fb23233b56ed34e8`,
two independent reviews accepted the second deterministic oracle with
P0/P1/P2 `0/0/0`. The controller ran only that case; it exited 1 after pytest
reported 0.69 seconds and failed exactly with `CORRUPT_RECORD` after the final
metadata check. No early unsafe error or alternate failure occurred.

The RED is accepted and consumed. Production may recognize an unlinked
replaceable FD only when its final stat remains a regular owner-owned 0600 file
on the store device with the same inode, size, and mtime as the initially safe
opened snapshot. Only the expected nlink transition to zero and its ctime
effect may differ. The complete bytes must still pass the existing bounded
read, canonical record, and checksum validation. Linked replaceable records and
all immutable revision records retain the strict metadata checks.

## TK5 Concurrent Path-Stat HEAD Rejection — TK5-E030

The E029 correction made all four deterministic/concurrent atomic-reader cases
GREEN. A new 100-iteration diagnostic still stopped at iteration 80 with
`UNSAFE_STORE`; an instrumented 300-iteration reproduction localized it to
the initial pathname stat in `_open_checked_file`. The HEAD stat returned the
old inode after atomic replacement had removed its directory entry, with all
trusted metadata intact except `st_nlink == 0`. The implementation rejected it
before opening or retrying the canonical pathname.

A third deterministic oracle now holds the old HEAD inode, atomically replaces
the pathname, returns the native unlinked old-inode stat for the first exact
HEAD pathname stat, and proves both injection and `nlink == 0`. It requires a
successful load but does not constrain retry count or old/new selection. Since
there is no content FD corresponding to this stale pathname-stat snapshot, the
safe behavior is to discard it and retry within the existing bounded open
budget. Immutable revision paths remain strict. The current production source
is frozen until independent static review and an exact predicted
`UNSAFE_STORE` RED complete.

## TK5 Deterministic Path-Stat HEAD RED — TK5-E031

At artifact/test/source anchors
`25c5106e25adc99e1c90e3e1f8400891ea9fe253d313bffb13e08b062193f1ea`,
`4c80d9419ed1e3fb25702ca74ac6744cc5ce6a7cdd09c20fff55262c4bf4b354`,
and `27f751f0163baa73f9aefff34355f42c4f85ce7dc5c6db90458b1c045a8b4dec`,
independent review accepted the third deterministic oracle with P0/P1/P2
`0/0/0`. The controller then ran only that case; it exited 1 after pytest
reported 0.70 seconds and failed exactly with `UNSAFE_STORE` at the initial
HEAD stat safety check. Injection and the native unlinked-inode observation
both completed before the failure.

The RED is accepted and consumed. The bounded replaceable-record open loop may
discard a trusted regular owner-mode-device pathname stat with `nlink == 0`
and retry the canonical name. It may not open or consume that stale snapshot,
and the same condition remains unsafe for non-replaceable revision content.

## TK5 Atomic-Reader Corrective GREEN — TK5-E032

At artifact/test/source anchors
`ff857c31e085716a231a7cdfc26ade032fb7dd7531978a65a77c23aac7e88c88`,
`4c80d9419ed1e3fb25702ca74ac6744cc5ce6a7cdd09c20fff55262c4bf4b354`,
and `9f3ad67fb1723f82b32d58dedab4428b19adefa087792b4b9b3009b45b1f40e5`,
relevant Ruff check/format check, Python compilation, and repository diff check
all exited 0. The three deterministic atomic-replacement cases, the original
pre-open replacement case, and the concurrent old-or-new reader case all
passed together. A separate 300-iteration real concurrent begin/seal/commit
diagnostic then completed with every reader observing only the complete old or
new HEAD and no exception.

The complete 24-case corrective selection exited 0 after pytest reported 0.42
seconds, with 24 passed and 176 deselected. The complete revision-store module
exited 0 after pytest reported 2.09 seconds, with all 200 cases passing. The
repository-wide suite exited 0 after pytest reported 17.06 seconds, with 1958
passed, 81 intentional slow cases deselected, and only the two known macOS
multithreaded-`fork()` deprecation warnings. Repository-wide Ruff check also
exited 0.

All dynamic gates are GREEN. Final acceptance still requires two fresh
independent full-file reviews at the exact test/source anchors above, each with
P0/P1/P2 `0/0/0`.

## TK5 Final Acceptance — TK5-E033

At artifact/test/source anchors
`dadb330d96c7f64c9fe56524dcf52596c402d23a6dbba1960caf074577088893`,
`4c80d9419ed1e3fb25702ca74ac6744cc5ce6a7cdd09c20fff55262c4bf4b354`,
and `9f3ad67fb1723f82b32d58dedab4428b19adefa087792b4b9b3009b45b1f40e5`,
two fresh independent full-file reviews returned unconditional ACCEPT with
P0/P1/P2 exactly `0/0/0`.

The first review re-derived FD ownership, single-attempt close behavior,
durable publish ordering, HEAD linearization, recovery evidence, lineage,
streaming integrity, and all three atomic replacement windows directly from
source. The second independently cross-checked every regression oracle against
the implementation, including exact replaceable-name scoping and retained
immutable-record constraints. Neither review changed or executed the candidate.

Together with E032's static, targeted, focused, stress, and repository-wide
GREEN evidence, this closes TK5. The accepted deliverable is the local immutable
CAD revision store plus its complete contract and regression suite. It adds no
dependency, network, model, MCP, FreeCAD, environment, or public UI mutation.
The exact commit message is
`feat(execution): persist immutable CAD revisions`; the branch must be pushed
non-force and verified clean and upstream-equal.

## TK5 Closeout and TK6 Resume — TK6-E000

### 1. Completed milestones

- TK5 was committed exactly as
  `4fe3c4c1100822dddb1262bd5603bac0a3d989b5`
  (`feat(execution): persist immutable CAD revisions`) and pushed non-force.
- The controller verified a clean `codex/task-kernel-phase2` branch with HEAD
  exactly equal to `origin/codex/task-kernel-phase2` at that commit.
- TK5 closed with 24/24 corrective tests, 200/200 revision-store tests, 1958
  full-suite passes, 81 intentional deselections, two known macOS fork
  deprecation warnings, 300/300 concurrent atomic-reader iterations, whole
  Ruff GREEN, and two independent P0/P1/P2 `0/0/0` reviews.
- The authoritative artifact at the committed closeout is
  `2012ceaebf15eb4eaef4566f96e2e17fb78937abfae4489aec22833b5303d332`.
  No source, test, dependency, environment, CAD, model, MCP, PR, or remote
  rewrite is active at this recovery point.

### 2. Next steps

1. Run the unchanged full baseline at the clean pushed TK5 anchor with only
   this approved artifact append present.
   - If the result differs from 1958 passes, 81 intentional deselections, and
     the two known macOS warnings without a demonstrated environment reason,
     stop TK6 as an unexpected gate RED.
2. Resolve the exact candidate/session ownership contract with bounded,
   independent, read-only reviews; the controller writes and freezes
   `tests/test_candidate_revision.py` before production exists.
   - If the contract requires a Session private field, server-global Session,
     public MCP change, new dependency, or a fourth named path, stop for TK-R2.
3. Execute one genuine missing-module RED, then implement only
   `src/vibecad/execution/candidate.py` against the frozen oracle.
   - If RED is not solely the absent module, source creation remains frozen.
4. Run focused, cumulative Stage C, full, pure-import, Ruff, format, diff,
   allowlist, and independent review gates.
   - Any invalid receipt that reaches HEAD commit, base/candidate object alias,
     rollback mutation of the committed binding, or ambiguous post-HEAD result
     is a circuit breaker.
5. Stage only the three TK6 files, commit the prewritten message, push
   non-force, and verify clean HEAD/upstream equality before TK7.

### 3. Approved decisions

- Active revision: TK-R1; authorization: TK-A02, exact user wording
  “批准 TK-R1，按计划开始 Stage C。”
- The user's standing direction to continue without internal product approvals
  and the current “还在继续推进吗？” request continuation within that already
  approved scope; neither expands authority.
- Active TK6 decisions: TK-D05–TK-D12, TK-D17–TK-D19, TK-D23, and TK-D24.
  TK-A03 remains controlling for recorded internal oracle/review corrections.
- TK6, its exact named files, gates, commit message, and non-force push were
  already ordered and approved. No duplicate approval is required.

### 4. Execution discipline

Capability profile re-evaluated for this resumed controller session:

    approval: native-plan
    delegation: spawn-send-wait
    persistence: repo-artifact
    process: native-session-poll

Adapter: Codex. Evidence uses exactly the permitted categories:

- live capability declarations
  - `update_plan` is declared live.
  - `spawn_agent`, `followup_task`, `send_message`, and `wait_agent` are
    declared live.
  - `exec_command` can return a session identifier and `write_stdin` can poll
    that exact session; `apply_patch` is declared for repository edits.
- observable behavior
  - Native plan projection accepted the TK5 closeout and TK6-resume state.
  - Spawn/follow-up/wait collaboration returned distinct TK5 implementation
    and independent review evidence in this controller thread.
  - A full-suite command returned a live session and was completed by polling
    the original session; no duplicate launch occurred.
- environment identity
  - Host/runtime: Codex desktop; controller: `/root`.
  - Workspace: `/Users/wangtao/Documents/DevProject/vibecad`.
- public configuration
  - Filesystem access is unrestricted for the declared workspace, network is
    enabled, and approval policy is `never` with no escalation path.

No capability fallback or environment residual is active. The artifact remains
authoritative and native plan remains a projection. TK6 uses
`spawn-send-wait`, standard implementation work, deep independent review, and
`native-session-poll` for any long gate. Its allowlist is only
`docs/orchestrated/vibecad-task-kernel-phase2.md`,
`tests/test_candidate_revision.py`, and
`src/vibecad/execution/candidate.py`. Unexpected RED, out-of-allowlist writes,
second unsuccessful focused GREEN, ambiguous process state, P0/P1, unresolved
P2, or architecture expansion stops the packet. Residuals remain deferred
unless they invalidate a gate.

## Task Packet TK6-P1 — Isolated Candidate Sessions

### 1. Authorization

TK-R1/TK-A02 explicitly approved TK6, the decisions and three-file allowlist
above, genuine RED, independent review, exact commit, and non-force push. This
packet inherits all higher-priority system, developer, and user instructions,
applicable directory-scoped instructions, the current host permission model,
and sandbox. Neither the Skill, artifact, nor packet grants or expands
permission, elevates authority, or bypasses those controls. Do not request the
same approval again.

### 2. Workspace anchor

Repository: `/Users/wangtao/Documents/DevProject/vibecad`.
Branch and upstream-equal anchor:
`codex/task-kernel-phase2@4fe3c4c1100822dddb1262bd5603bac0a3d989b5`.
No applicable repository `AGENTS.md` or `CLAUDE.md` exists. Modify only the
three TK6 allowlisted files. Do not alter execution package exports, Session,
revision/validation/workflow modules, server/MCP surfaces, dependencies,
environment, Git history, or remote configuration. The current permission
model and sandbox remain binding.

### 3. Context

TK5 provides immutable candidate staging, sealed revision publication, atomic
HEAD, and reconciliation. TK4 provides authentic verification receipts. TK6
must join those capabilities to injected CAD Session ownership without running
a ModelProgram or collecting geometry; those remain TK7/TK8.

An active candidate owns a newly created or base-loaded Session distinct from
the committed Session binding. Checkpoint produces controller-owned FCStd
staging, seal reloads that exact snapshot as a read-only verification Session,
and commit consumes a successful receipt before HEAD advancement. Rollback
closes only candidate/sealed Sessions and never reloads, mutates, or replaces
the committed baseline. After HEAD advances, cleanup or SessionSlot promotion
failure reports committed recovery state and never rolls HEAD back.

Success requires exact-once terminal behavior, lease and HEAD/base rechecks,
receipt binding, immutable sealed content, SessionSlot compare-and-swap, and
deterministic reconcile results. No Session private field, server-global
Session, dynamic callable discovery, user/model path, FreeCAD import, network,
retry loop, or public API exposure is permitted.

### 4. Steps and gates

1. Verify the clean TK5 baseline and current environment fingerprint.
2. Resolve strict immutable value schemas, `CadSnapshotPort` method boundary,
   Session binding/slot CAS, coordinator state machine, error taxonomy, receipt
   consumption, revision-store call order, close ownership, and recovery
   semantics with independent static reviews.
3. Write `tests/test_candidate_revision.py` first. Static review must freeze
   the public direct-module surface and prove fake base/candidate/sealed Session
   separation, rollback isolation, exact-once terminal calls, forged/replayed/
   mismatched/failed receipt rejection, changing HEAD, seal-after-write,
   checkpoint/load/close/CAS faults, and pre-/post-HEAD error classification.
4. Run exactly one focused RED accepted only when collection fails solely
   because `vibecad.execution.candidate` is absent.
5. Implement only `candidate.py`; run focused GREEN, then cumulative TK1–TK6,
   Phase-1 C1–C5 compatibility, full normal suite, pure-import and dependency
   boundaries, Ruff check/format, Python compilation, diff/hash/allowlist, and
   two fresh complete independent reviews.
6. Stage the exact three files, repeat staged gates, commit exactly
   `feat(execution): isolate candidate sessions`, push non-force, and verify
   clean upstream equality.

### 5. Execution discipline

Delegation is `spawn-send-wait`; implementation tier is standard and review
tier is deep where selectors are used. Long processes use
`native-session-poll`; never relaunch an active command. The controller owns
artifact/test/source edits, executable tests, staging, commit, push, and final
acceptance. Review workers are read-only and do not run Python, pytest, Ruff,
CAD, network/model services, or Git mutation.

Use one missing-module RED and at most two focused GREEN attempts. Bound all
record counts, diagnostic text, coordinator calls, close attempts, and terminal
transitions. Stop on an unexpected baseline/RED, unverifiable oracle, any
aliasing of committed and candidate Session objects, mutation after sealing,
receipt bypass, HEAD rollback after linearization, guessed recovery, private
Session access, out-of-allowlist write, or scope/authority expansion.

### 6. Delivery boundary

Complete one independently accepted, pushed TK6 commit and a clean recovery
snapshot. The controller reserves executable validation, acceptance, exact
staging, commit, and push. TK6 does not execute ModelProgram steps, observe
geometry, export STEP, orchestrate TaskRun, expose MCP, install/upgrade, or run
real FreeCAD. Those remain TK7–TK9.

### 7. Final report

Return exact artifact/test/source hashes and line counts, focused/cumulative/
full/static gate commands and numeric results, independent P0/P1/P2 verdicts,
any justified deviation or residual, commit/push state, and final workspace
status. No silence, elapsed wait, or prose-only claim counts as evidence.

## TK6 Baseline Evidence — TK6-E001

At clean pushed TK5 executable anchor
`4fe3c4c1100822dddb1262bd5603bac0a3d989b5`, with only the approved TK6
artifact append at
`4a4eefb90f76f3f693576312785fcb95a1a5598d99099edcd20f3e5bcbff3d23`,
the controller verified that both TK6 source and test paths were absent and
ran `PYTHONPATH=src .venv/bin/pytest -q` through one native session. It exited
0 after pytest reported 17.08 seconds with exactly 1958 passed, 81 intentional
deselections, and the same two known macOS multithreaded-`fork()` deprecation
warnings. Repository status and diff check were clean except for the named
artifact append. No CAD, install, dependency, model, MCP, source, test, staging,
commit, push, or other external mutation occurred.

The baseline matches E032 exactly and authorizes bounded read-only TK6 contract
resolution. Production and tests remain frozen until the controller records
the resolved surface and independently accepted oracle strategy.

## TK6 Resolved Candidate Contract — TK6-E002

### Contract review and correction

Two bounded, independent, read-only compatibility reviews inspected the full
TK6 packet and the current Session, lease, revision-store, validation, and
execution boundaries. The first candidate was ACCEPT with P0/P1/P2 `0/0/0`,
but the later close-order review produced more specific evidence and therefore
supersedes that provisional verdict with REJECT `0/3/2` before oracle freeze.
The findings are internal correctness corrections authorized by TK-A03, not a
product or scope change:

1. A staging FCStd must be checkpointed and reloaded before TK7 exports STEP;
   `seal_revision` cannot run first because TK5 requires the STEP input.
2. If durable seal succeeds but closing the staging-reloaded Session fails,
   HEAD is still old and the operation must terminally reconcile as
   not-committed. It must not issue a commit-capable handle.
3. After HEAD advancement, SessionSlot ownership must be derived from both the
   CAS outcome and a read-back identity check. A return value alone cannot
   authorize closing either Session.
4. Terminal results carry orthogonal durable, promotion, cleanup, and recovery
   facts rather than asking one status value to encode every dimension.
5. Cleanup identity is retained without permitting receipt, commit, CAS, or
   close replay. A close attempt is never repeated after a side-effect-then-
   raise result, and a current live slot Session is never cleanup-owned.

This section closes those findings in the oracle. No fourth path, dependency,
Session edit/private field, concrete FreeCAD adapter, STEP exporter, public
package export, server-global Session, MCP, model, network, install, or product
decision is introduced. The corrected oracle may now be written; production
remains absent until the genuine RED.

### Exact direct-module surface

`vibecad.execution.candidate` exports only:

    CandidateErrorCode, CandidateError,
    CandidateCommitStatus, CandidateRollbackStatus,
    CandidateReconcileStatus, CadSnapshotPort, SessionBinding, SessionSlot,
    ActiveCandidate, CheckpointedCandidate, SealedCandidate,
    CandidateCommitResult, CandidateRollbackResult,
    CandidateReconcileResult, CandidateCoordinator

The execution package root remains unchanged. There are no free commit,
rollback, reconcile, serialization, mapping, dynamic discovery, or adapter
functions.

`CadSnapshotPort` is the trusted injected base boundary with exactly four CAD
responsibilities:

    create_empty(*, revision_id: str) -> object
    load_fcstd(path: Path) -> object
    checkpoint_fcstd(session: object, path: Path) -> None
    close(session: object) -> None

It does not export STEP, hash files, import FreeCAD, discover callables, choose
paths, or expose Session internals. A create/load implementation owns and must
clean any partial Session until it returns one object; return transfers that
Session to the coordinator.

`SessionBinding` is frozen, slotted, keyword-only, identity-equality-only, and
non-serializable, with exact fields `project_id`, `revision_id`, and `session`.
`SessionSlot(initial)` requires one exact non-null binding and belongs to that
project. Its only public instance methods are `current()` and
`compare_and_set(expected, replacement)`. CAS compares `current is expected`,
never invokes binding or Session equality, rejects a different project and a
replacement Session alias, and publishes at most once under its lock.

The three candidate handles are frozen, slotted, keyword-only, identity-
equality-only, non-serializable process capabilities. Their exact fields are:

- `ActiveCandidate`: `project_id`, `base_head`, `binding`, `model_path`,
  `step_path`.
- `CheckpointedCandidate`: the same five fields, where `binding` owns the
  independently staging-reloaded read-only export Session.
- `SealedCandidate`: `project_id`, `base_head`, `revision`, and `binding`, where
  `binding` owns the independently immutable-revision-reloaded verification
  Session.

Direct construction, copy, replacement, serialization, a stale transition
handle, or a handle from another coordinator cannot authorize an operation;
the coordinator registry authenticates exact object identity. Candidate
handles never expose the committed baseline binding.

`CandidateCoordinator` is constructed keyword-only with one exact
`LocalRevisionStore`, one trusted `CadSnapshotPort`, and one exact
`SessionSlot`. Its public lifecycle is:

    begin(*, project_id, expected_head, lease) -> ActiveCandidate
    checkpoint(*, candidate, lease) -> CheckpointedCandidate
    seal(*, candidate, lease) -> SealedCandidate
    commit(*, candidate, receipt, compiled, snapshot, lease)
        -> CandidateCommitResult
    rollback(*, candidate, lease) -> CandidateRollbackResult
    reconcile(*, project_id, lease) -> CandidateReconcileResult

The caller owns and releases the original `ProjectWriteLease`; the coordinator
never acquires, replaces, steals, or releases it. Every later operation
requires the exact same live lease object captured by begin, and the revision
store performs the authoritative issuer/HEAD check before any CAD or receipt
side effect.

### Results and errors

All result values are frozen, slotted, keyword-only, and non-serializable.
Commit status is `committed`, `committed_cleanup_required`, or
`committed_recovery_required`; rollback status is `not_committed`,
`cleanup_required`, or `recovery_required`; reconcile status is `clean`,
`committed`, `not_committed`, `cleanup_required`, or `recovery_required`.

`CandidateCommitResult` contains `schema_version`, `status`, `head`,
`revision`, `live_binding`, `report`, `head_committed`, `slot_promoted`,
`cleanup_required`, `recovery_required`, and `cleanup_binding`.
`CandidateRollbackResult` contains `schema_version`, `status`, `head`,
`live_binding`, `reconciliation`, `head_committed`, `slot_promoted`,
`cleanup_required`, `recovery_required`, and `cleanup_binding`.
`CandidateReconcileResult` has the same fields as rollback. Nullable head,
binding, reconciliation, and cleanup identity are explicit only where durable
or live state could not be read safely. Invariants reject contradictory result
construction.

`CandidateError` has a closed fixed-message taxonomy: `invalid_input`,
`invalid_identifier`, `invalid_candidate`, `invalid_transition`,
`invalid_binding`, `invalid_lease`, `session_alias`, `terminal_in_progress`,
`already_terminal`, `receipt_rejected`, `cad_failure`, `store_failure`,
`conflict`, `cleanup_required`, and `recovery_required`. It also reports the
orthogonal booleans `head_committed`, `cleanup_required`, and
`recovery_required`; it never reflects an identifier, path, Session, receipt,
or underlying exception text. Underlying trusted exceptions may remain only as
local exception causes.

### Lifecycle, identity, and exact-once order

1. `begin` strictly validates exact public values, the slot's current project
   and revision, current HEAD, and immutable base revision. `begin_revision`
   atomically rechecks lease and HEAD. An imported base loads only the fixed
   candidate model path; an empty base calls `create_empty` with the internally
   generated revision ID. The returned Session must differ by identity from
   the baseline. Any post-begin fault closes only a returned non-baseline
   Session and reconciles the staging journal before returning an error.
2. `checkpoint` revalidates exact lease, HEAD, slot, and fixed store-owned
   paths; writes FCStd once; reloads that staging FCStd into a second,
   non-aliased Session; and only then closes the active Session once. It returns
   a `CheckpointedCandidate`. Any save/load/alias/close fault terminally closes
   remaining owned Sessions and reconciles not-committed, with no retry.
3. TK7, not TK6, may use only the checkpointed read-only Session to export STEP
   to the exposed fixed `step_path`. No ModelProgram operation is valid on that
   handle.
4. `seal` calls TK5 `seal_revision`, reloads `revision_model_path` into a third
   non-aliased Session, and only then closes the checkpoint/export Session. It
   returns `SealedCandidate` only after all three succeed. Missing STEP, seal,
   reload, alias, or close failure remains pre-HEAD and terminally reconciles
   not-committed; no commit capability escapes.
5. Observation and verification use only the immutable-reloaded sealed
   Session. `commit` reserves terminal ownership, rechecks exact lease, HEAD,
   and `slot.current is baseline`, then calls only
   `consume_verification_receipt` with the exact revision and manifest. Invalid,
   failed, forged, mismatched, or replayed receipt reaches no HEAD mutation.
6. Receipt success is terminal even if the following store commit fails. Every
   pre-HEAD failure automatically closes remaining candidate-owned Sessions and
   reconciles not-committed; the old slot Session is never touched and the
   consumed receipt is never reused.
7. HEAD replacement is the only commit linearization point. A successful
   commit or `DURABILITY_UNCERTAIN(head_committed=True)` can never call rollback.
   The coordinator performs at most one baseline-to-sealed CAS, then reads the
   slot identity before deciding ownership.
8. A successful transition retires the prior handle. A terminal operation is
   reserved before its first side effect. Duplicate or concurrent terminal
   calls either receive the bounded cached same-operation result or a fixed
   terminal error; they never re-consume, recommit, re-reconcile, re-CAS, or
   re-close.

### Post-HEAD CAS and cleanup matrix

- CAS `True` and current is replacement: `slot_promoted=True`; ownership has
  transferred and sealed is never closed. Close the displaced baseline once.
- CAS `False` and current is baseline: `slot_promoted=False`; baseline remains
  live and sealed may be closed once; recovery is required.
- CAS raises and current is replacement: promotion occurred; never close
  sealed, report recovery because the adapter result was ambiguous, and close
  the displaced baseline once.
- CAS raises and current is baseline: promotion did not occur; never close
  baseline, close sealed once, and report recovery.
- CAS `False` but current is replacement: treat the identity read-back as
  promoted, never close sealed, and report recovery for the contradictory CAS.
- CAS `True` but current is not replacement, or current is a third binding, or
  current cannot be read: ownership is unknown. Close neither baseline nor
  sealed, set `slot_promoted=None`, and report recovery.

Any close is marked attempted before invocation and is never retried after an
exception. `cleanup_binding` identifies only an unattempted, non-live binding
that a later bounded reconciliation may clean. It never identifies the current
slot binding, never authorizes closing a side-effect-uncertain prior attempt,
and never authorizes receipt, HEAD commit, or CAS replay.

### Reconciliation

`reconcile` calls `store.reconcile` first; HEAD, journal, and immutable records
are the durable truth. It then reads the exact slot binding:

- Matching slot revision and reconciled HEAD is stable and performs zero CAS or
  close.
- A committed journal that exactly proves old HEAD to candidate permits one
  load of the immutable model and one identity CAS from the captured old
  binding. The full post-HEAD CAS/read-back matrix applies.
- Not-committed or cleanup-required leaves the old slot untouched and closes
  only same-coordinator candidate-owned Sessions not previously attempted.
- Clean durable state with a mismatched slot, a divergent third binding,
  corrupt/ambiguous evidence, or failed identity read-back is recovery-required
  and never guessed into consistency.

Process restart cannot recreate an Active/Checkpointed/Sealed capability.
Reconcile may use durable HEAD/journal evidence and the currently captured slot
identity only. A sealed-but-not-committed immutable revision may remain a valid
orphan; that is not a committed live Session.

### Frozen oracle matrix and residuals

The controller test owns: exact surface/signatures/value invariants; hostile
input and forged/cross-coordinator handles with zero side effects; invalid,
foreign, and released lease ordering; empty/imported begin; baseline/active/
checkpoint/sealed identity separation; fixed paths; staging reload before STEP;
missing STEP; immutable reload before observation; save/load/close/seal faults;
HEAD/slot changes; forged/failed/wrong/replayed receipts; receipt consumption
order; pre-/post-HEAD store faults; every CAS/read-back branch; cleanup identity;
explicit rollback; concurrent/duplicate terminals; and the complete durable
reconciliation matrix. Static tests forbid Session private names, FreeCAD,
server, MCP, network, dynamic discovery, user paths, STEP synthesis, dependency
changes, package-root exports, and serialization surfaces.

Residuals do not invalidate TK6: the concrete Session port, STEP exporter,
trusted observation executor, real FreeCAD proof, TaskService orchestration,
and server slot composition remain TK7–TK9/Stage 3; a consumed receipt cannot be
reused after a pre-HEAD fault; legal sealed orphans can remain; process-local
handles do not survive restart; a close side-effect-then-raise cannot be safely
retried; and no public task/MCP surface exists yet.

## TK6 Durable-Truth Oracle Correction — TK6-E003

An independent read-only adversarial oracle review challenged E002 before any
production existed. Its P0 durable-truth finding is accepted under TK-A03 and
supersedes any E002 wording that could be read as trusting a commit exception's
code or `head_committed` metadata:

- After a receipt has been consumed, every exception from `commit_revision`,
  including an ordinary exception and contradictory durability metadata, is an
  ambiguous outcome until the coordinator calls durable reconciliation.
- `store.reconcile` and an exact durable HEAD read are authoritative. A
  reconciled committed candidate enters the post-HEAD CAS path and can never
  roll back, even if the exception claimed pre-HEAD failure. A reconciled
  not-committed candidate closes only candidate-owned Sessions and terminates
  with the baseline slot untouched, even if the exception claimed the HEAD was
  committed.
- If reconciliation and exact HEAD cannot agree on old versus candidate, the
  operation terminates recovery-required. It does not replay receipt, commit,
  rollback, CAS, or an already-attempted close and does not guess ownership.
- A genuine pre-HEAD fault oracle must wrap the real store before the HEAD
  mutation and prove `consume → commit fault → durable reconcile → old HEAD`.
  A genuine post-HEAD oracle must execute the real commit and then throw a
  generic exception; acceptance requires discovery of the advanced HEAD and
  completion of the post-HEAD SessionSlot path.

CAS is likewise judged by exact slot read-back identity after every `True`,
`False`, or raised result. Only the exact replacement binding establishes
promotion; only the exact old binding establishes non-promotion. A divergent
or unreadable slot is recovery-required. Closing a Session still follows the
more conservative E002 ownership rule: if it might have crossed the live slot
boundary, no close is guessed.

The oracle uses authentic `compile_acceptance_spec`, `verify_acceptance`, and
`consume_verification_receipt` behavior. It proves a successful receipt is
burned after both a successful commit and a later pre-HEAD store fault; a
binding mismatch that fails before receipt consumption remains consumable on
its original exact binding, even though the candidate lifecycle itself is
terminally rolled back under TK-D21. No mocked or constructed receipt can make
a commit test GREEN.

The review also requires repeated reconciliation to be side-effect stable and
an explicit post-HEAD displaced-baseline close-failure case. The former may
return the same semantic facts without repeating load, CAS, or close; the
latter is committed with `slot_promoted=True`, `cleanup_required=True`, and no
sealed-Session close or HEAD rollback. These corrections close the adversarial
P0/P1 oracle findings without expanding TK6's API, allowlist, or authority.

## TK6 Test-Oracle Review Corrections — TK6-E004

Two independent full-file reviews rejected the initial 1177-line oracle at
test/artifact anchors
`344797c4c288c782dca02f7744e7303fafcf390908c10d8af7dc3ace2eccfda5`
and
`bdd2cc90879b92f89abb9add9aef1881c448b4593ec32248a8ed6acabd361bf9`.
The verdicts were P0/P1/P2 `1/5/3` and `2/5/3`. Production remained absent
and no RED was spent. The controller accepts the overlapping durable-truth,
precondition-order, ownership-leak, receipt-matrix, reconcile, concurrency, and
value/static findings and expands the pre-production oracle before review.

The corrected test design now additionally requires:

- old durable HEAD despite falsely claimed `head_committed=True`, and new
  durable HEAD despite falsely claimed `head_committed=False`;
- one real durable reconcile after every accepted commit exception, zero
  rollback replay, and recovery when reconciled classification and exact HEAD
  disagree;
- wrong, foreign, and released lease, changed HEAD, and changed exact slot
  rejection before receipt consumption or HEAD mutation, with the authentic
  receipt still consumable on its original binding;
- absent, forged, failed, wrong-compiled, wrong-revision, wrong-manifest,
  mismatched-snapshot, and replayed authentic receipt cases;
- begin/create/load alias and fault cleanup, checkpoint alias and secondary
  Session cleanup, immutable reload alias/fault cleanup, and all close attempts
  counted by object identity;
- every CAS outcome plus exact replacement, exact baseline, divergent, and
  unreadable read-back, with exactly one CAS and conservative close ownership;
- fresh-coordinator committed recovery, not-committed zero-load/zero-CAS
  idempotence, cleanup/recovery mapping, CAS-failure then reconcile convergence,
  and rollback confronted with an already advanced durable HEAD;
- commit-versus-commit, commit-versus-rollback, and rollback-versus-rollback
  terminal races; and
- exact fields, keyword-only construction, frozen slots, identity equality,
  result invariants, nonserialization, fixed nonreflective errors, cross-project
  slot rejection, concurrent one-winner CAS, and provenance-based static
  boundaries for imports, dynamic execution, Session internals, and file writes.

The slot-retirement finding is closed without adding a public method:
`SessionSlot` may maintain a private weak retirement registry and the
coordinator atomically marks a non-live candidate binding retired before
closing it. Public CAS rejects that exact retired binding, so a stale
`SealedCandidate.binding` cannot later publish its closed Session. Retirement
and the “is this binding/session current?” check share the slot lock; if a
binding may already be live, the coordinator does not close it. Constructing a
new binding around a Session and mutating the slot outside the coordinator and
project lease remains a violation of the trusted same-process boundary, not an
untrusted ingress capability; the all-public-write ownership policy remains
TK-R14/Stage 3.

One implementation constraint is also explicit. TK5 has no non-mutating public
issuer-validation operation for a PREPARED journal. Therefore a sealed commit
can check the exact captured lease object, its public project/liveness, current
HEAD, and exact slot before receipt consumption; `commit_revision` performs
the authoritative issuer/process check immediately afterward. A released or
foreign lease is rejected before receipt. A process-fork-invalid lease can
consume a receipt before the store rejects it, after which durable reconciliation
terminates safely; avoiding that fail-closed receipt burn would require a new
lease/store API outside TK6's allowlist. This host/process residual does not
weaken HEAD or Session safety and is deferred rather than hidden.

## TK6 Precondition and Oracle Unification — TK6-E005

The fresh reviews of the E004 oracle again returned REJECT before RED:
P0/P1/P2 were `2/6/2` and `0/4/3`. The controller accepts the concrete
coverage findings and resolves one wording ambiguity before another review.

E005 supersedes E002's broad phrase “every pre-HEAD failure” as follows. A call
has not entered the candidate terminal operation until exact handle ownership,
exact captured live lease, expected durable HEAD, and exact baseline slot
identity all pass. A forged/cross-coordinator/stale handle, foreign/released/
replacement lease, changed HEAD, or changed slot is a rejected precondition:
it performs zero CAD, receipt, commit, rollback, reconcile, CAS, or close and
leaves the authentic sealed candidate available for a corrected call or an
explicit rollback. Terminal reservation occurs immediately after those checks
and before receipt consumption. From that point, receipt rejection or any CAD/
store failure is an accepted candidate failure and terminally closes/reconciles
without retry. This is consistent with TK-D21: invalid invocation authority is
not a verification/execution failure, while an accepted invalid verification
capability is.

The genuine pre-HEAD oracle no longer replaces `commit_revision`. It runs the
real LocalRevisionStore commit path through all validation and prepared-record
checks, then injects an I/O result, ordinary exception, or falsely committed
durability exception at TK5's internal HEAD replacement seam before replacement.
It requires exactly one subsequent durable reconciliation, old HEAD, zero
rollback replay, a burned authentic receipt, terminal handle, and closed sealed
Session. Post-HEAD claimed-true, claimed-false, and generic exceptions likewise
each require exactly one real store reconciliation and zero rollback. Both
directions of reconciliation-versus-HEAD disagreement are fail-closed.

Session retirement is an ownership operation, not just a stale-handle error.
Before every coordinator-owned Session close, the coordinator asks the slot
under the slot lock to atomically retire the exact binding only if neither that
binding nor its Session is current. Failure to retire means the Session may be
live and cannot be closed. The active binding is retired before checkpoint
close, the checkpoint binding before seal close, every candidate binding before
rollback/terminal cleanup, and the displaced baseline before post-promotion
close. Public CAS rejects retired bindings. Tests invoke CAS from inside the
port's close hook, after each transition, after receipt failure, after explicit
rollback, and after promotion to prove retire-before-close ordering. This does
not authorize arbitrary external slot mutation; all legitimate CAS remains
inside the project-lease ownership discipline.

The next oracle revision also adds: forced overlap while a winning terminal
operation is blocked inside its first real store side effect; rollback-wins
proof that receipt remains unconsumed; same-issuer replacement-lease rejection;
all True/False/raise plus unreadable commit read-backs; fresh committed
reconcile load/CAS/divergent/unreadable ownership cases; forged checkpointed and
sealed handles; cleanup/recovery repeat stability; and an exact import allowlist
plus direct dynamic-execution/file-write prohibitions. A loser may report either
`terminal_in_progress` or `already_terminal`; neither permits a second side
effect.

## TK6 Third Oracle Correction — TK6-E006

The E005 fresh reviews again stopped before RED. Their exact verdicts were
P0/P1/P2 `1/3/1` and `1/4/2`. The shared P0 was an oracle bookkeeping defect:
a forged-handle assertion compared the port log captured before a legitimate
rollback with the log after that rollback. The comparison anchor is now
recaptured after rollback. No production or executable RED occurred.

All shared P1 corrections are incorporated. Receipt rejection and explicit
rollback now attempt stale publication from inside the fake port's close hook,
proving retire-before-close rather than merely checking afterward. The forced
terminal race no longer requires a loser thread to remain alive because an
immediate `terminal_in_progress` is valid. The fresh committed-reconcile matrix
now independently covers load failure, baseline alias, false/raise at baseline,
false/raise after replacement publication, true without publication,
false/true/raise with a divergent binding, and true/false/raise with unreadable
read-back. Each case proves CAS count and exact baseline/replacement close
ownership. Checkpoint and seal also reject a new same-issuer lease after the
captured lease is released.

The source boundary now combines an exact import allowlist with bans on direct
`open`, `write`, `replace`, rename/unlink/touch/mkdir, convenience writes,
dynamic import/execution, and Session private/document access. Candidate.py
therefore cannot synthesize STEP or write arbitrary content; only the injected
port and existing revision store own such effects. SessionSlot's public callable
surface remains exactly `current` and `compare_and_set`; retirement stays a
private coordinator/slot ownership operation.

## TK6 Frozen-Test Final Acceptance — TK6-E007

The controller completed the remaining pre-RED correction and convergence
rounds without creating or importing the production module and without running
the candidate tests. The final oracle closes constructor-parameter rebinding,
Session implicit-protocol and carrier reflection paths, hidden mutable module
or class state, unbounded strong retirement records, arbitrary CAD path
construction or derivation, lifecycle-external dunder hooks, public dependency
fields, unused-lock false greens, and the previously open CandidateError
surface. It also replaces name-only path checks with provenance from the exact
authenticated candidate or LocalRevisionStore path APIs, and proves all Slot
and Coordinator state access occurs inside the corresponding direct RLock
region.

The final frozen candidate is:

- `tests/test_candidate_revision.py` SHA-256
  `3e4ac0ff1e397426e79a5b4ddb3ad817968f232f6bd6be6d0fce8bf7b1a99e2e`,
  6342 lines;
- this artifact before E007 SHA-256
  `3b2b00d635e2594eeea4c5d923e89945f83cef295969775e735cacf5d98e8b62`,
  8541 lines;
- repository HEAD
  `4fe3c4c1100822dddb1262bd5603bac0a3d989b5`; and
- `src/vibecad/execution/candidate.py` absent.

Static pre-RED gates passed for the final candidate: Ruff format, Ruff check,
Python byte-compilation, and diff whitespace validation. Three separate
read-only reviews then accepted these exact anchors with P0/P1/P2 `0/0/0`:
one lifecycle/oracle convergence review, one ordinary code-quality review, and
one implementation-compatibility review. They performed no pytest run, Python
execution, project import, or repository write. The review circuit breaker is
therefore closed and the next permitted executable evidence is the single
genuine missing-module RED.

## TK6 Genuine Missing-Module RED — TK6-E008

With the E007 oracle frozen, the controller ran exactly:

    PYTHONPATH=src .venv/bin/pytest -q tests/test_candidate_revision.py

The command exited `2` during collection in `1.17s`. Its only error was:

    ModuleNotFoundError: No module named 'vibecad.execution.candidate'

Pytest reported one collection error and executed no candidate test. This is
the required genuine absent-production RED; no fallback, placeholder, partial
module, or unrelated failure contributed to it. The artifact immediately
before E008 was SHA-256
`b8d71c6eef01655b2a478cb202773a40971ab83fb2f6b38671452703f69b8fb8`
at 8576 lines. Production implementation is now permitted within the unchanged
TK6 three-file allowlist and the two-attempt focused GREEN budget.

## TK6 Fixture-Gate Correction — TK6-E009

The first post-implementation focused invocation did not reach candidate
behavior. Pytest reported `6 passed, 159 failed in 29.25s`; every failure arose
during `_rig` setup because the new strict lease and revision roots had not
been created before their constructors ran. The coordinator and its lifecycle
assertions were therefore not exercised. This invocation is classified as an
invalid fixture/environment gate, not either of the two permitted focused
GREEN attempts.

The fixture now creates every temporary `ResourceLeaseManager` and
`LocalRevisionStore` root through one private helper that uses mode `0700` and
then explicitly applies `chmod(0700)`, making the required permission exact
under the host's `umask 022`. No assertion, parameter matrix, identity check,
or side-effect oracle was weakened. Ruff format, Ruff check, Python
byte-compilation, and both standalone structural candidate oracles pass.

The corrected frozen anchors are:

- `tests/test_candidate_revision.py` SHA-256
  `719492f33bc7ab59ae26bfae8fc84413bd43df96a4d8c8481bcb1e6c09ecca21`,
  6351 lines;
- `src/vibecad/execution/candidate.py` SHA-256
  `5677801a63c41ed75f6026eb6b28c807c3ed57d6d29158a01884bcbbeef4dd0b`,
  1774 lines; and
- this artifact before E009 SHA-256
  `005d002e0948b54d40c99b7d862b4bb357da06d33b8c04ba5464cc4dcc81e7a9`,
  8594 lines.

An independent read-only fixture review accepted the corrected test anchor
with P0/P1/P2 `0/0/0` and confirmed that all constructor roots are covered.
Pre-GREEN implementation review also identified and corrected a deterministic
ownership defect: divergent third-binding reconciliation now closes retained
candidate Sessions without closing a still-authoritative pending baseline.

## TK6 Focused GREEN — TK6-E010

Two pre-GREEN read-only implementation reviews converged after one additional
exact-once correction. Accepted pre-HEAD faults previously attempted
`rollback_revision` and then, if that call raised, attempted `reconcile`; that
could cross two durable terminal boundaries. `_abort` now makes exactly one
rollback attempt, never falls through to a second durable call, and reports a
durable exception or committed outcome as `RECOVERY_REQUIRED`. A durable
cleanup outcome or Session close failure reports `CLEANUP_REQUIRED`; otherwise
the originating CAD, store, alias, or receipt error remains unchanged. Both
the exact-once/lifecycle reviewer and the implementation-compatibility reviewer
accepted the corrected implementation with P0/P1/P2 `0/0/0`.

The first valid focused GREEN invocation then ran exactly:

    PYTHONPATH=src .venv/bin/pytest -q tests/test_candidate_revision.py

Pytest executed all 165 frozen candidate tests and reported:

    165 passed in 3.65s

There were no skips, xfails, retries, or environment/fixture failures. This is
focused GREEN attempt 1 of the two-attempt budget; attempt 2 remains unused.
The accepted anchors are:

- `src/vibecad/execution/candidate.py` SHA-256
  `79d4392c4b0d22eee1006aa7e01a62c293e08f6b9ddfc6904204e6803408bb20`,
  1792 lines;
- `tests/test_candidate_revision.py` SHA-256
  `719492f33bc7ab59ae26bfae8fc84413bd43df96a4d8c8481bcb1e6c09ecca21`,
  6351 lines; and
- this artifact before E010 SHA-256
  `a836e0f71b08a9391a103bbf6538ed9d0c3cf961f7105a731cb060f17ca5f961`,
  8629 lines.

## TK6 Cumulative, Full, and Static GREEN — TK6-E011

The cumulative TK1–TK6 plus Phase-1 compatibility command covered task state,
workflow contracts, execution registry, ModelProgram validation, tool-result
normalization, execution adapter, project leases, task store, acceptance
verification, immutable revisions, and isolated candidates. It exited 0 with
`1630 passed, 1 deselected in 10.82s`.

The unchanged full normal repository command
`PYTHONPATH=src .venv/bin/pytest -q` then exited 0 with
`2123 passed, 81 deselected, 2 warnings in 21.63s`. The 165 new TK6 cases
account exactly for the increase from the 1958-test clean TK5 baseline. The two
warnings are the already accepted macOS multi-threaded-`fork()` deprecations in
`tests/test_workflow_lease.py`; there are no new warnings, skips, xfails,
errors, or failures.

Whole-repository Ruff check, relevant-file Ruff format check, Python
byte-compilation, fresh pure import, forbidden-module scan, whitespace diff,
three-path allowlist, branch identity, and upstream-anchor checks all exited 0.
Pure import loaded none of FreeCAD, Part, MCP, model SDK, network, or socket
modules. The only dirty paths are the three TK6 allowlisted files, and HEAD and
upstream remain equal at
`4fe3c4c1100822dddb1262bd5603bac0a3d989b5` on
`codex/task-kernel-phase2`.

The exact pre-review anchors are:

- artifact SHA-256
  `5be6de188e43afb7fd40bbe611db82a1634394fda35bef5f3563dab533c01ae1`,
  8664 lines before E011;
- source SHA-256
  `79d4392c4b0d22eee1006aa7e01a62c293e08f6b9ddfc6904204e6803408bb20`,
  1792 lines; and
- test SHA-256
  `719492f33bc7ab59ae26bfae8fc84413bd43df96a4d8c8481bcb1e6c09ecca21`,
  6351 lines.

## TK6 Final-Review Recovery Repair — TK6-E012

The two final complete read-only reviews rejected the E011 implementation with
P0/P1/P2 `0/2/0` and `0/1/0`. They independently identified the same durable
begin boundary: a `begin_revision` exception after persistence did not perform
one authoritative reconcile, while a later candidate-path failure swallowed
the outcome of its one rollback attempt. The lifecycle review additionally
found that divergent-slot retained cleanup discarded a Session close failure.
No public API, normal lifecycle, HEAD linearization, or product decision was
affected.

The controller added bounded regression oracles for three begin-reconcile
outcomes, both fixed path getters across three rollback outcomes, and both
commit/fresh divergent bindings with and without close side-effect failure. An
independent test-only review first caught and corrected an invalid
`DURABILITY_UNCERTAIN` fixture constructor. The first targeted invocation then
reported 11 failures and 2 passes; two failures exposed an oracle assumption
that terminal `NOT_COMMITTED` journals disappear. LocalRevisionStore preserves
that terminal journal, so later reconcile stably returns `NOT_COMMITTED`.
Changing only those two final-state assertions produced the independently
accepted test anchor. The corrected targeted RED reported exactly `9 failed,
4 passed in 2.55s`, all at the three reviewed production gaps.

Production now uses exactly one durable boundary for each accepted begin
failure. If the revision id is unknown, it reconciles once; if known, it rolls
back that exact revision once. `NOT_COMMITTED` preserves `STORE_FAILURE`,
`CLEANUP_REQUIRED` reports its matching attention error, and an exception or
committed outcome reports `RECOVERY_REQUIRED`. A pre-begin `load_revision`
read failure remains a plain store failure and performs no durable mutation.
Divergent retained-candidate cleanup now propagates its close-failure boolean
without closing the still-authoritative pending baseline or retrying a
side-effecting close.

Evidence after the repair:

- targeted regression group: `13 passed in 0.43s`;
- second and final focused GREEN: `176 passed in 3.86s`;
- cumulative TK1–TK6 plus Phase-1 compatibility: `1641 passed, 1 deselected
  in 10.93s`;
- full normal repository: `2134 passed, 81 deselected, 2 warnings in 20.51s`;
  the warnings remain only the two accepted macOS fork deprecations; and
- whole Ruff, relevant format, Python compilation, pure/forbidden import,
  whitespace, and exact three-path allowlist gates all passed.

The final pre-review anchors are:

- artifact before E012 SHA-256
  `cb94d86de35a5b3a6c9ce6a1dacf904743f1c34777ae1e2760bb5ba49f23e492`,
  8701 lines;
- source SHA-256
  `d9c37f95e4a50a51102854fa81b7031a155cb8021a522e5928f84867a8aa27fc`,
  1837 lines; and
- test SHA-256
  `6adb738aa29fa7b48fda8b8851dc57cdc495964e7c2955c91b4e7bc07e83a23b`,
  6483 lines.

## TK6 Final Independent Acceptance — TK6-E013

Two fresh complete read-only reviews inspected the exact E012 artifact, source,
test, and HEAD anchors. The lifecycle/concurrency review and the API/dependency
review both returned ACCEPT with P0/P1/P2 `0/0/0`. They confirmed closure of
both prior durable-begin findings and the divergent cleanup-reporting finding,
including exact-one reconcile-versus-rollback selection, attention flags,
retire-before-close ownership, baseline preservation, and the new non-vacuous
regression matrix.

They also accepted the unchanged closed public surface, identity-only
capabilities, fixed redacted errors, store-derived path provenance, pure import
boundary, and compatibility evidence. Remaining non-blocking residuals are the
already declared process-local capability lifetime, non-retryable
side-effect-then-raise close, legal sealed orphan recovery case, coarse
coordinator RLock, and the concrete FreeCAD/STEP/observation/TaskService work
reserved for TK7–TK9. No source, test, artifact, dependency, environment, CAD,
network, model, MCP, or Git mutation occurred during either review.

## TK6 Commit, Push, and TK7 Start — TK7-E001

TK6 was staged through its exact three-file allowlist, committed as
`54522681ea9cc79bd183b0e9637a37b9a72aa043` with the approved message
`feat(execution): isolate candidate sessions`, and pushed non-force. The local
branch, upstream branch, and clean worktree then matched exactly before TK7.

TK7 freezes one internal `InProcessCadExecutor` that also implements the
`CadSnapshotPort` required by TK6. It exposes only program validation, the
four fixed default-operation bindings, exact-once execution, controlled STEP
export, public Session checkpoint/load/close operations, and trusted sealed
evidence collection. Export uses only the store-derived candidate STEP path;
evidence re-reads the immutable RevisionRef, independently hashes and detects
the FCStd/STEP formats, and reads geometry directly from the sealed Session.
Neither tool-returned text nor StepResult evidence can enter the acceptance
snapshot. There is no configurable handler registry, output directory, retry,
dynamic discovery, model path, server-global Session, model call, or network
surface.

The initial frozen test candidate is `tests/test_program_executor.py`. It
covers the fixed/redacted contract, authentic validation, public Session port,
four-handler binding, ordered stop-on-first-failure behavior, zero retries,
candidate/lease/path authority, export failure, geometry ownership, immutable
artifact hashes, FCStd/STEP signature detection, mutation between trusted
reads, malformed/non-finite shape facts, and immutable evidence values. The
next permitted executable evidence is the single genuine absent-module RED;
`src/vibecad/execution/executor.py` remains absent.

## TK7 Frozen Oracle and Independent Pre-RED Acceptance — TK7-E002

Three independent read-only reviews examined the final TK7 oracle without
running Python, pytest, CAD, or network code and without modifying the
repository. Their earlier P1 findings were closed by non-vacuous tests for
`CadSnapshotPort` composition, exact handler kwargs and all-handler preflight,
partial-load cleanup, store-rejected leases, wrong lifecycle stages, unsafe
symlink/directory/hardlink export entries, same-size SHA-256 mutations, deep
FCStd/STEP signatures, first-read and actual-file mutation, untrusted inspect
facts, boundary redaction, a second independent geometry fact set, and exact
TaskArtifactRef lineage and metadata. The compatibility, contract, and
security reviews then each returned ACCEPT with P0/P1 `0/0`.

The frozen pre-RED anchors are:

- `tests/test_program_executor.py` SHA-256
  `3738c73c39c22aa86f02f3fe513b57ffd447eba6a39f2a4743868015afe5059f`,
  1068 lines;
- this artifact before E002 SHA-256
  `b98d887f5f5951835f433a3eec045f527e7c1c81f10529bf52e9b0046e352921`,
  8807 lines; and
- repository HEAD/upstream
  `54522681ea9cc79bd183b0e9637a37b9a72aa043` with the executor module absent.

Static Ruff check/format, Python byte-compilation, and diff whitespace checks
passed for the frozen test candidate. The next and only permitted executable
test evidence is the genuine missing-module RED.

## TK7 Genuine Missing-Module RED — TK7-E003

The controller ran exactly:

    PYTHONPATH=src .venv/bin/pytest -q tests/test_program_executor.py

Pytest exited `2` during collection in `0.25s`. Its only error was
`ModuleNotFoundError: No module named 'vibecad.execution.executor'`; no TK7 test
executed and no unrelated failure contributed. This is the required genuine
absent-production RED. The frozen oracle remains unchanged at SHA-256
`3738c73c39c22aa86f02f3fe513b57ffd447eba6a39f2a4743868015afe5059f`.
Production implementation is now permitted within the unchanged TK7
three-file allowlist and two-attempt focused GREEN budget.

## TK7 Focused, Regression, and Real-CAD GREEN — TK7-E004

The first focused GREEN executed all 44 frozen TK7 cases and reported
`44 passed in 0.27s`. Independent implementation review then requested
defense-in-depth classification and cleanup improvements, without identifying
a product-level decision: nonblocking artifact opens, broad fixed preflight
redaction, distinct CAD classification for shape acquisition, and safe
best-effort cleanup of a failed ordinary STEP leaf. The second and final
focused invocation reported `44 passed in 0.23s`; no test or public contract
changed, and no retry was introduced.

Final regression evidence after those improvements is:

- cumulative TK1–TK7 plus Phase-1 compatibility: `1685 passed, 1 deselected
  in 11.02s`;
- full normal repository suite: `2178 passed, 81 deselected, 2 warnings in
  20.67s`; both warnings are the already accepted macOS multi-threaded-fork
  deprecations in `tests/test_workflow_lease.py`;
- whole-repository Ruff check, relevant-file Ruff format, Python compilation,
  pure import, forbidden-module scan, diff whitespace, and exact three-path
  allowlist checks passed; and
- the existing opt-in FreeCAD adapter smoke reported `1 passed, 58 deselected
  in 1.04s` using only the installed ready environment.

A bounded direct installed-FreeCAD smoke additionally created a 10×20×30 Box,
checkpointed FCStd, closed and reloaded it, modified length to 12, checkpointed
to the same candidate pathname, closed and reloaded again, and independently
confirmed bbox `(12, 20, 30)`, volume `7200`, validity, and one solid. It exited
0 with `executor-real-fcstd-smoke-ok`. Its first harness construction used an
untrusted temporary lease-root ancestry and was correctly rejected before CAD;
the corrected port-only smoke used an exact inert LocalRevisionStore instance,
created no environment, performed no install/download/upgrade, and confined
all artifacts to its auto-removed temporary directory.

The final anchors before artifact E004 are:

- `src/vibecad/execution/executor.py` SHA-256
  `9323295b26d1f91f02c9db5ac26b48ed14c29d76fdbae0f522fb78390bb9b29b`,
  666 lines;
- frozen `tests/test_program_executor.py` SHA-256
  `3738c73c39c22aa86f02f3fe513b57ffd447eba6a39f2a4743868015afe5059f`,
  1068 lines; and
- this artifact before E004 SHA-256
  `77d6d15821e2b3aae481c2d1f4b66445c78bbd212da8744753595c5f8d34a3ee`,
  8849 lines.

## TK7 Final Independent Acceptance — TK7-E005

Three independent complete read-only implementation reviews converged on
ACCEPT with P0/P1 `0/0`. They confirmed the exact TK8 composition order,
fixed-handler adapter reuse, public Session-port compatibility, candidate and
lease gates, controlled artifact path, immutable manifest re-read, direct
sealed geometry ownership, fixed errors, zero retry, and partial STEP cleanup.
The apparent checkpoint concern was withdrawn after confirming that this is a
disposable candidate staging copy: any failure rolls the candidate back and
cannot alter the immutable baseline revision or live committed binding.

Accepted P2 residuals are limited to (1) a same-UID malicious pathname/ABA race
that requires violating the approved TRUSTED_LOCAL, private-0700, held-lease,
trusted-same-process boundary, and (2) ZIP central-directory parsing before the
post-open entry-count budget. Neither surface is model-controlled or publicly
exposed in Stage C. TK8 must preserve the no-callback interval between
seal/immutable reload, evidence collection, verification, and commit.

## TK7 Commit, Push, and TK8 Contract Resolution — TK8-E001

TK7 was committed as `2a5aebbf61a1bdd119e9250e25835f728099ff7f`
with the exact message
`feat(execution): collect trusted CAD verification evidence` and pushed
non-force. The local branch, upstream branch, and clean worktree then matched
exactly before TK8.

Three independent TK8 composition reviews found two contradictions in the
earlier service-only packet. First, a rolled-back candidate cannot enter
`NEEDS_INPUT` and later bind a different revision in the same schema-v1
TaskRun: its audit histories are intentionally bound to one candidate, while
the terminal coordinator capability and removed staging revision cannot be
reused. Second, `CandidateCoordinator.begin` may require cleanup or recovery
before it can return a candidate revision, while the earlier
`VALIDATING_PROGRAM` state could not persist either attention outcome.

TK8 resolves these without weakening isolation, reusing a revision, deleting
diagnostics, or adding a repair loop:

- TK-D33 — In TK-R1, only failures before a candidate is durably published may
  produce `NEEDS_INPUT`. Once `VALIDATE_PROGRAM` has bound a candidate,
  execution, geometry, policy, cancellation, and required-verification
  failures roll back to `FAILED`, even if a low-level StepError carries
  `needs_input=true`. A caller that wants a revised plan creates a new TaskRun;
  multi-attempt lineage is deferred to a future schema rather than silently
  overwriting the one-candidate audit record. This supersedes only the
  candidate-origin `NEEDS_INPUT` clause of TK-D21.
- TK-D34 — `VALIDATING_PROGRAM` may enter `RECOVERY_REQUIRED` or
  `CLEANUP_REQUIRED` before a candidate revision is published. A successful
  reconcile proving that no candidate was committed uses the new explicit
  `CONFIRM_PRE_CANDIDATE` event to return to `PROGRAM_READY`, clearing the
  attention error; retry still requires an explicit caller `continue_task`.
  `CLEANUP_REQUIRED` may escalate to `RECOVERY_REQUIRED` if reconciliation
  discovers ambiguity.
- TK-D35 — A candidate returned by `begin` is a private prepare result until
  the `VALIDATE_PROGRAM` CAS publishes its revision. If that CAS cannot be
  confirmed, TaskService invokes exactly one coordinator rollback before
  releasing the lease and records a pre-candidate conflict when task storage
  is available. No ModelProgram command has executed, and the unpublished
  staging attempt is not represented as a durable candidate. Every failure
  after the publish CAS still enters durable `ROLLING_BACK` before rollback.
- TK-D36 — Mutating service calls require an explicit expected TaskRun
  generation. `DURABILITY_UNCERTAIN` is accepted only after exact generation
  and TaskRun readback; semantic CAD, receipt, commit, and rollback calls are
  never repeated to resolve a task-store write.

The TK8 named-file packet therefore adds the already-approved overall-allowlist
paths `src/vibecad/workflow/state.py` and `tests/test_task_state.py` to
`service.py`, `test_task_service.py`, and this artifact. The nine-commit budget,
public/MCP boundary, model policy, dependency set, and TK9 scope are unchanged.
This is an internal consistency repair under the user's standing instruction
to continue without internal approval pauses; it does not introduce a new
product capability or public behavior surface.

## TK8 State Repair and Genuine RED — TK8-E002

The TK-D33/TK-D34 state repair was first frozen in executable tests. Candidate
failures can no longer transition from `ROLLING_BACK` to `NEEDS_INPUT`; an
input hint on a published candidate remains diagnostic data but the attempt
finishes `FAILED`. Pre-candidate cleanup/recovery attention has explicit origin
guards, serialized transition provenance checks, cleanup-to-recovery
escalation, and `CONFIRM_PRE_CANDIDATE` clears the attention error before an
explicit retry. The focused state suite passed `434` tests.

Before any service implementation existed, the new transactional service
oracle passed Ruff, formatting, byte-code compilation, and `git diff --check`.
The exact command
`PYTHONPATH=src .venv/bin/pytest -q tests/test_task_service.py` then failed only
during collection with
`ModuleNotFoundError: No module named 'vibecad.workflow.service'`. This is the
genuine absent-module RED for TK8. The oracle includes the unpublished
begin/publish window, exact and non-exact durability-uncertain readback,
post-publication CAS conflict, coordinator-internal abort, rollback attention,
post-HEAD final-CAS recovery, and no-recommit reconciliation cases before the
production module is introduced.

## TK8 Transactional GREEN, Store Repair, and Final Acceptance — TK8-E003

`TaskService` now composes the exact TaskRunStore, LocalRevisionStore,
ResourceLeaseManager, CandidateCoordinator, InProcessCadExecutor, and trusted
acceptance verifier as one internal transactional boundary. The successful
path durably publishes every state transition, step result, artifact, and
verification report before the one permitted candidate commit. Pre-candidate
failure remains retryable through an explicit caller action; every published
candidate failure durably rolls back to `FAILED`; post-HEAD ambiguity never
recommits or rolls back. The service remains direct-module-only and adds no
MCP, network, model, callback, repair loop, semantic retry, or public tool
surface.

Real TaskRunStore composition review exposed and closed three fake-hidden
defects. These decisions supersede the narrower TK3 assumptions only where
stated:

- TK-D37 — Task records accept finite canonical Python JSON float tokens
  because ModelProgram, StepResult, and VerificationReport legitimately carry
  binary64 values. NaN and infinities remain forbidden, envelope integer
  fields remain exact integers, and canonical byte equality rejects alternate
  numeric spellings. Every create, compare-and-set, and `validate_record`
  performs the same encode-then-full-decode self-check before any lease or file
  mutation. This supersedes TK3's blanket float-free wording without changing
  its duplicate-key, checksum, canonical JSON, or bounded-resource guarantees.
- TK-D38 — A submitted program must fit both the 512 KiB service budget and the
  complete durable TaskRun decode envelope before the first SUBMIT CAS.
  Oversized strings, node floods, excessive depth, or domain decode-budget
  violations return fixed `INVALID_INPUT` while the original generation and
  `NEEDS_PLAN`/`NEEDS_INPUT` state remain unchanged. Store create and CAS use
  the identical self-check, so later step or verification values can never
  replace a readable record with an unreadable one.
- TK-D39 — Project reconciliation journal state describes only the latest
  project transaction and is not a TaskRun commit certificate. After exact
  durable HEAD and live SessionBinding agreement, TaskService walks at most
  256 immutable RevisionRef parents and validates the HEAD manifest, candidate,
  and exact task base record. A passing report bound to the immutable candidate
  confirms success even after later descendants replace HEAD; reaching the
  verified base first confirms non-commit. Missing, malformed, cyclic, broken,
  or over-budget ancestry remains `RECOVERY_REQUIRED`.
- TK-D40 — `VALIDATING_PROGRAM` is a recoverable crash state. Reconcile first
  acquires the project lease, then durably marks recovery, then asks the
  coordinator to settle either a no-begin attempt or a private begin result
  that crashed before candidate publication. Exact pre-candidate settlement
  returns to `PROGRAM_READY` and clears the attention error.

The final non-vacuous evidence is:

- focused state/store/service group: `673 passed in 2.95s`;
- cumulative TK1–TK8 plus Phase-1 compatibility: `1770 passed, 1 deselected
  in 11.97s`;
- full normal repository: `2263 passed, 81 deselected, 2 warnings in 23.71s`;
  the warnings are only the two accepted macOS multi-threaded-fork
  deprecations;
- whole-repository Ruff, six-file format, Python byte-compilation, pure import,
  forbidden-module, whitespace, exact branch/upstream, and seven-path
  allowlist gates passed; and
- a real TaskRunStore test traversed float-bearing ModelProgram, StepResult,
  and VerificationReport values from submit through terminal reload. Finite
  values including `-0.0`, the minimum subnormal, and maximum finite binary64
  retain their exact `float.hex()` representation.

Independent store and service/recovery reviews both returned ACCEPT with
P0/P1 `0/0`. The final service review additionally ran the complete Store +
Service pair (`238 passed`) and confirmed the validating crash window,
candidate/base ancestry, missing-base fail-closed behavior, and write-before-
side-effect ordering. The accepted residuals are the deliberately narrower
durable subset of the in-memory domain contract, same-CPython canonical float
representation, and the 256-revision recovery walk budget; all reject or hold
for recovery before unsafe mutation.

The pre-E003 anchors are:

- artifact SHA-256
  `e4cbe50dde80df602b88d193c177ce89916b786badfd8f690e75773eed9e59c4`,
  8995 lines;
- state/store/service SHA-256 respectively
  `af74511fceb6ad228a6218593190153e8b9bc4a2284e6e36fecffe123f1a7867`,
  `a7225b78c1deadc3701cca4fc85e836ccc3b4f462dbd29f9eba89d4931790d84`,
  and `a60b8f851a3dba61d2d03b6b6eed8c19dabc83dd4fd2927e3fa4978abb33757d`;
- state/store/service test SHA-256 respectively
  `3bb4a1cf9c07b696b0d38a367385d77bc919b2f0c05b842433f2cc07916c4a03`,
  `05da8ccd5e8b354fbec82a0bcd10802ecf190849799384099475f817f790309b`,
  and `46e5e104cdf8f3c6d843b33e1a88f376971e9f2598844c89b532a504e6d0a246`.
