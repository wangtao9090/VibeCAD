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
- TK-D12 — After execution, the candidate is checkpointed to private staging,
  reloaded as the exact sealed FCStd, and only trusted read-only observation and
  export operations run on that sealed content. The final manifest hashes the
  model and artifacts.
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
  repair/replan loops. label_expired, conflict, or a structured needs_input
  failure rolls the candidate back and returns needs_input. Geometry, runtime,
  policy, cancellation, and required verification failures roll back to failed.
  R-B16 prevents broader tool-specific retry policy until stable semantic tool
  error codes exist.
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

- src/vibecad/workflow/service.py
- tests/test_task_service.py
- docs/orchestrated/vibecad-task-kernel-phase2.md

Deliver:

- Internal TaskService.create_task, submit_model_program, continue_task,
  get_task, and reconcile_task.
- Exact order:
  request budget and contract checks; acceptance compile; program validation;
  TaskRun CAS; project lease; HEAD/base recheck; candidate begin; execution;
  checkpoint/reload; controlled export; trusted observation; verification;
  committing record; candidate commit or rollback; final TaskRun CAS.
- Policy for needs_input, failed, durability_uncertain, cleanup_required, and
  recovery_required. No automatic repairing state is entered in TK-R1.

Gates:

1. Genuine RED for the absent service.
2. Fake-port coverage of every state and failure edge, including proof that
   rejection before candidate creation performs no project/CAD call.
3. Store-CAS conflict and project-writer contention; recovery after each
   commit window; candidate-present failures always traverse rolling_back.
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
