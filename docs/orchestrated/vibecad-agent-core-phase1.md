# VibeCAD Agent Core Phase 1 Orchestrated Campaign

- Campaign: `vibecad-agent-core-phase1`
- Active revision: `R1`
- Status: `R1 approved / execution authorized`
- Prepared: `2026-07-16`
- Repository anchor: `main@9af2714c799bb34ca59907514c15cee0db7645f7`
- Current working version: `0.4.0` (uncommitted over a `0.3.0` HEAD)
- Target branch after approval: `codex/agent-core-phase1`
- Push policy: push the target branch after each accepted commit; never force-push and never push directly to `main`
- Pull-request policy: not authorized by this revision

This file is the authoritative execution, approval, evidence, recovery, and
handoff record for this campaign. Sections are append-only after approval.

## Capability Profile

```text
approval: native-plan
delegation: spawn-send-wait
persistence: repo-artifact
process: native-session-poll
```

### Adapter evidence record

- `live capability declarations`
  - Native plan projection is available through `update_plan`.
  - Delegation supports `spawn_agent`, `send_message`, `followup_task`, and
    `wait_agent`.
  - Repository edits are available through `apply_patch`.
  - Long-running commands can return a live session and be polled through
    `write_stdin`.
  - No native durable-memory interface is declared.
  - No worker model-selection parameter is declared by `spawn_agent`.
- `observable behavior`
  - Native plan projection has accepted plan state in this workspace session.
  - Spawn/wait collaboration has returned bounded repository-inspection results
    in this workspace session.
  - No additional state-changing capability probe was required.
- `environment identity`
  - Host: Codex desktop.
  - Workspace: `/Users/wangtao/Documents/DevProject/vibecad`.
  - Filesystem: workspace-write with repository metadata readable and project
    files writable.
- `public configuration`
  - none observed

The host does not expose per-worker model selection, so this campaign does not
claim that fast/standard/deep worker-model routing has been applied. This is a
performance limitation, not a correctness exception.

## Campaign Context

The accepted target architecture is documented in
`docs/AGENT_ARCHITECTURE.md`. VibeCAD is a vertical CAD Agent that can be called
by external hosts such as Codex or Claude. It does not sell managed model
tokens. Exactly one reasoning owner is active for a run:
`external_plan`, `mcp_sampling`, or `byok`.

The first implementation slice is deliberately below the model layer. It
establishes versioned workflow contracts, safe-operation registration,
pre-execution validation, result normalization, and one real FreeCAD adapter
proof. It adds no model SDK, provider key, photo-to-mesh engine,
mesh-to-parametric-CAD engine, simulation engine, or arbitrary Python execution
path.

The working tree is not currently a reproducible base: it contains an
interdependent, uncommitted VibeCAD `0.4.0` implementation over a `0.3.0` HEAD.
The baseline must therefore be reviewed, gated, committed, and pushed before
new Agent Core implementation begins.

---

## Stage A — Stabilize the Existing 0.4.0 Baseline

### 1. Context

There are 32 modified tracked files and 9 untracked files at revision R1. They
cover the managed FreeCAD runtime, CAD project and measurement tools, modeling
integrity, release checks, tests, and documentation. Agent Core commits made on
top of an uncommitted base would not be independently reproducible or safely
reviewable.

Stage A normalizes only the existing changes. It must not add Agent Core
behavior.

### 2. Decisions

- `D-A01` — Preserve all current user changes; do not reset, discard, or
  rewrite unrelated work.
- `D-A02` — Treat the interdependent runtime/CAD/test/config changes as one
  pre-existing `0.4.0` integration unit. This exception avoids constructing an
  intermediate commit whose manifest, version, server registrations, or tests
  are knowingly inconsistent.
- `D-A03` — Keep release-workflow validation and documentation in separate
  commits because each can be reviewed and gated independently.
- `D-A04` — Create `codex/agent-core-phase1` only after explicit approval of
  revision R1. Never force-push or push `main`.
- `D-A05` — Every commit uses named-file staging, a staged-diff review, an
  independent reviewer, and immediate branch push after acceptance.
- `D-A06` — If review shows that any current file is unrelated, secret-bearing,
  destructive, or cannot safely belong to this baseline, stop and issue R2
  instead of silently including it.

### 3. Commit Sequence

#### `B1` — `feat: complete the VibeCAD 0.4.0 runtime and CAD baseline`

Purpose: capture the already-present, interdependent runtime, CAD, integrity,
manifest, dependency, and test changes as a reproducible release baseline.

Named files:

- `manifest.json`
- `mcpb_entry.py`
- `pyproject.toml`
- `uv.lock`
- `src/vibecad/__init__.py`
- `src/vibecad/engine/session.py`
- `src/vibecad/runtime/installer.py`
- `src/vibecad/runtime/spec.py`
- `src/vibecad/runtime/status.py`
- `src/vibecad/server.py`
- `src/vibecad/tools/__init__.py`
- `src/vibecad/tools/features.py`
- `src/vibecad/tools/measure.py`
- `src/vibecad/tools/modeling.py`
- `src/vibecad/tools/project.py`
- `src/vibecad/tools/sketch.py`
- `tests/test_engine_session.py`
- `tests/test_installer.py`
- `tests/test_launcher_uninstall_integration.py`
- `tests/test_mcpb_manifest.py`
- `tests/test_runtime_integration.py`
- `tests/test_server_new_tools.py`
- `tests/test_server_round10.py`
- `tests/test_server_tools.py`
- `tests/test_session_parts.py`
- `tests/test_status.py`
- `tests/test_supervisor.py`
- `tests/test_tools_measure.py`
- `tests/test_tools_modeling.py`
- `tests/test_tools_project.py`
- `docs/orchestrated/vibecad-agent-core-phase1.md`

Gates:

1. G0: inventory named files; inspect full and staged diffs; check for secrets,
   generated artifacts, accidental broadening, and version drift.
2. G1: run the focused tests corresponding to changed modules.
3. G2: run the complete normal test suite and lint suite.
4. G3: run the repository's opted-in FreeCAD integration suite using the
   installed FreeCAD runtime.
5. Independent reviewer checks scope, contracts, error paths, and test evidence.
6. Stage named files only, commit, push branch, and append evidence below.

#### `B2` — `ci: verify release version consistency`

Named files:

- `.github/workflows/release.yml`
- `.github/scripts/check_release_versions.py`
- `tests/test_release_workflow.py`
- `docs/orchestrated/vibecad-agent-core-phase1.md`

Gates:

1. G0 staged-diff and workflow-permission review.
2. G1 release-workflow and manifest/version tests.
3. Independent reviewer checks trigger safety, permissions, and version-source
   consistency.
4. Commit, push, and append evidence.

#### `B3` — `docs: document VibeCAD 0.4.0 and the target agent architecture`

Named files:

- `PRIVACY.md`
- `README.md`
- `docs/ACCEPTANCE_TESTS.md`
- `docs/AGENT_ARCHITECTURE.md`
- `docs/ARCHITECTURE.md`
- `docs/USER_GUIDE.md`
- `docs/superpowers/plans/2026-07-02-agent-orchestrator-prototype-draft.md`
- `docs/superpowers/specs/2026-07-02-vibecad-agent-architecture-design.md`
- `docs/orchestrated/vibecad-agent-core-phase1.md`

Gates:

1. G0 staged-diff review, link/path validation, and supersession consistency.
2. Independent reviewer checks that current and target architecture are not
   conflated and that user-facing claims match implemented behavior.
3. Confirm a clean working tree, commit, push, and append evidence.

### 4. Manual Validation Matrix

| Check | Owner | Mode | Evidence | Required before close |
|---|---|---|---|---|
| Inspect tool manifest and reported version | implementer | automated + readback | command output in ledger | yes |
| Start the real FreeCAD-backed integration path | implementer | automated integration | test command, exit status, FreeCAD version | yes |
| Review architecture claims against shipped tools | independent reviewer | document/code review | reviewer verdict | yes |

No user-present GUI validation is required for Stage A. If the repository's
existing integration suite cannot exercise the installed runtime without GUI
interaction, that gap becomes a residual rather than an invented pass.

### 5. Budget and Circuit Breakers

- Commit budget: 3 commits.
- Repair budget: at most 2 gate attempts per commit before stop-and-replan.
- Review budget: one independent implementation review per commit; unresolved
  high-severity findings stop the stage.
- Stop immediately if:
  - a file outside the allowlist changes during execution;
  - a secret, credential, private key, or personal data is detected;
  - the current diff cannot be explained as VibeCAD `0.4.0` work;
  - normal tests or real FreeCAD integration fail for a reason that cannot be
    repaired within the named scope;
  - a safe staged split would produce a knowingly invalid repository state;
  - branch push would require force or direct `main` mutation.

### 6. File Allowlist

Stage A may modify only the files explicitly named in commits `B1`–`B3`.
Changes to the campaign artifact are limited to approval, evidence, decisions,
residuals, and recovery records. No generated CAD artifact, cache, local Python
environment, or FreeCAD installation directory may be committed.

### 7. Expected Impact

- Converts the current VibeCAD `0.4.0` workspace into a reviewable and
  reproducible branch baseline.
- Preserves the already-present runtime, CAD, release, test, and documentation
  work.
- Establishes clean provenance before Agent Core files are introduced.
- Does not change the planned Agent Core behavior by itself.

### 8. Residuals

| ID | Residual | Disposition | Closure criterion |
|---|---|---|---|
| `R-A01` | Current baseline was dirty and unpushed | **closed** by B1–B3 review, push, and clean-tree verification | closed at `f2e60875b4c8fb6944cad1c0c75abe518015be82` |
| `R-A02` | Exact origin of every pre-existing hunk has not been independently reviewed | inspect during G0 | reviewer accounts for all named-file diffs |
| `R-A03` | PR creation is not authorized | intentional | user separately authorizes a PR |
| `R-A04` | Python 3.13.14 skips Hatch's hidden editable `.pth`, so bare `uv run` cannot import this checkout | gate with `PYTHONPATH=src`; defer toolchain choice | separately approved packaging/toolchain fix makes bare README command pass |
| `R-A05` | Interrupted isolated slow fixture may leave ignored `.vibecad-test-runtime` cache data | do not delete without a cleanup decision | user authorizes cleanup or cache is intentionally reused |
| `R-A06` | `Session` has no explicit lock or thread-owner assertion | require serialized caller behavior for now; user decision later | execution coordinator serializes access or Session gains tested ownership/locking |
| `R-A07` | FCStd load/recompute runs synchronously without size, timeout, cancellation, or process isolation | do not expand B1; security/robustness design needed | approved hostile/large-file isolation contract and tests |
| `R-A08` | External/nested `App::Part` interpretation and empty-part save/load semantics are not fully specified | preserve current behavior; user decision later | explicit import policy and round-trip matrix approved |
| `R-A09` | Empty undo/redo history mutation semantics are not redesigned in B1 | callers already guard counts; defer | Session-level no-op/error contract is approved and tested |
| `R-A10` | FreeCAD `closeDocument()` could theoretically perform its side effect and then raise; strict application rollback would then be impossible | identity guard + publish-after-close; accepted non-blocking engine residual | isolated document worker/transaction design removes global close ambiguity |
| `R-A11` | GitHub reports the repository moved from lowercase `vibecad.git` to canonical `VibeCAD.git` while redirecting successfully | do not mutate user remote during this campaign | user approves remote URL normalization |
| `R-A12` | Release actions use mutable major/release tags and MCPB is acquired by runtime `npx` | accepted pre-existing supply-chain residual; no network execution in B2 | approved SHA/checksum pinning policy and automation |
| `R-A13` | PyPI setup/build/publish share one job with job-scoped `id-token: write` | accepted P3; standard trusted-publishing shape | approved split build-artifact/publish-job hardening |

---

## Stage B — Agent Core Phase 1: Stable Execution Contracts

### 1. Context

Stage B begins only after `R-A01` is closed. Its goal is to make the middle of
the architecture real without selecting or embedding a model. The output is a
small, testable internal contract layer that later adapters, skills, MCP hosts,
BYOK runners, photo/mesh providers, and deterministic FreeCAD execution can all
share.

### 2. Decisions

- `D-B01` — Implement domain contracts with the Python standard library only.
  Phase 1 adds no model or validation framework dependency.
- `D-B02` — All persisted or cross-boundary workflow objects carry
  `schema_version = 1`; unsupported major versions fail closed.
- `D-B03` — The operation registry contains only explicit semantic CAD
  operations. Arbitrary Python/FreeCAD code execution is not registered.
- `D-B04` — Model-program validation is deterministic and runs before any CAD
  side effect. It validates structure, references, argument shape, budgets, and
  allowed operations.
- `D-B05` — Tool outcomes normalize to a common success/error/evidence envelope
  without changing the existing public MCP response schema in this phase.
- `D-B06` — Core workflow and execution modules must remain importable without
  importing FreeCAD or starting the runtime.
- `D-B07` — A thin injected adapter proves the contract against existing
  FreeCAD-backed operations. If this requires editing existing server/tool
  implementations, stop and issue a new plan revision.
- `D-B08` — Exactly one reasoning owner remains an architectural invariant, but
  no reasoning backend is implemented in Stage B.

### 3. Commit Sequence

#### `C1` — `feat(workflow): define versioned agent contracts`

Named files:

- `src/vibecad/workflow/__init__.py`
- `src/vibecad/workflow/contracts.py`
- `src/vibecad/workflow/errors.py`
- `tests/test_workflow_contracts.py`
- `docs/orchestrated/vibecad-agent-core-phase1.md`

Required behavior:

- Define typed, versioned representations for intent, acceptance criteria,
  model-program commands, execution evidence, and normalized outcomes.
- Round-trip supported objects to/from plain mappings.
- Reject unknown versions, invalid enum values, and malformed required fields.

Gates: genuine RED test first; focused test; import-without-FreeCAD test; lint;
independent contract review; named-file staged review; commit and push.

#### `C2` — `feat(execution): register safe semantic CAD operations`

Named files:

- `src/vibecad/execution/__init__.py`
- `src/vibecad/execution/registry.py`
- `tests/test_execution_registry.py`
- `docs/orchestrated/vibecad-agent-core-phase1.md`

Required behavior:

- Register explicit operation metadata, argument requirements, risk class, and
  whether verification evidence is required.
- Reject duplicates, unknown operations, and arbitrary-code operation names.
- Keep the registry independent of FreeCAD imports and network/model clients.

Gates: genuine RED test first; focused test; import boundary test; lint;
independent safety review; staged review; commit and push.

#### `C3` — `feat(workflow): validate model programs before execution`

Named files:

- `src/vibecad/workflow/program.py`
- `tests/test_model_program.py`
- `docs/orchestrated/vibecad-agent-core-phase1.md`

Required behavior:

- Validate command IDs, dependency references, operation allowlisting,
  argument shape, duplicate IDs, cycles, and command-count budget.
- Return deterministic, structured validation failures with stable error codes.
- Guarantee that validation has no CAD side effect.

Gates: genuine RED test first; focused tests including cycle/unknown-operation/
budget failures; lint; independent adversarial review; staged review; commit and
push.

#### `C4` — `feat(execution): normalize CAD tool outcomes`

Named files:

- `src/vibecad/execution/results.py`
- `tests/test_tool_result_normalizer.py`
- `docs/orchestrated/vibecad-agent-core-phase1.md`

Required behavior:

- Normalize successful values, structured tool errors, unexpected exceptions,
  elapsed time, warnings, and evidence references.
- Redact exception internals from the public message while preserving a local
  diagnostic classification.
- Never reinterpret an error as success.

Gates: genuine RED test first; focused success/error/exception tests; lint;
independent error-contract review; staged review; commit and push.

#### `C5` — `test(execution): prove the contract against the FreeCAD adapter`

Named files:

- `src/vibecad/execution/adapter.py`
- `tests/test_execution_adapter.py`
- `docs/orchestrated/vibecad-agent-core-phase1.md`

Required behavior:

- Execute a validated command through an injected mapping of existing semantic
  tool callables.
- Reject execution if validation was not successful or an operation is absent.
- Demonstrate one real FreeCAD-backed create/inspect flow in an opted-in slow
  test, including normalized evidence.
- Do not modify existing server, tool, session, installer, or manifest files.

Gates: genuine RED test first; focused unit tests; real FreeCAD slow test; full
normal test suite; lint; independent integration review; staged review; commit
and push.

### 4. Manual Validation Matrix

| Check | Owner | Mode | Evidence | Required before close |
|---|---|---|---|---|
| Inspect serialized workflow contract for stability and readability | independent reviewer | readback | reviewer verdict | yes |
| Confirm invalid programs cause no CAD call | implementer + reviewer | automated spy test | focused test output | yes |
| Create and inspect one shape through the real injected FreeCAD adapter | implementer | opted-in integration | command, exit status, normalized result | yes |
| Confirm existing MCP tool list and response behavior remain compatible | implementer | regression suite | full-suite output | yes |

No user-present manual validation is required because this phase exposes no new
UI or public MCP tool. A later public Agent workflow stage will require a
separate approval and validation matrix.

### 5. Budget and Circuit Breakers

- Commit budget: 5 commits.
- Total campaign budget: 8 commits.
- Repair budget: at most 2 gate attempts per commit before stop-and-replan.
- Delegation budget: one bounded implementer and one distinct reviewer per
  commit; additional workers require a recorded reason.
- Stop immediately if:
  - Stage A is not clean and pushed;
  - a Stage B change requires editing an existing server/tool/runtime/manifest
    file;
  - implementation adds a model SDK, credential, network call, arbitrary-code
    executor, or provider engine;
  - validation can invoke CAD operations or produce side effects;
  - FreeCAD is imported during core-module import;
  - a commit exceeds its named-file scope;
  - the campaign would exceed 8 commits without an approved revision;
  - a required gate remains failing after the repair budget.

### 6. File Allowlist

Stage B may modify only:

- `src/vibecad/workflow/__init__.py`
- `src/vibecad/workflow/contracts.py`
- `src/vibecad/workflow/errors.py`
- `src/vibecad/workflow/program.py`
- `src/vibecad/execution/__init__.py`
- `src/vibecad/execution/adapter.py`
- `src/vibecad/execution/registry.py`
- `src/vibecad/execution/results.py`
- `tests/test_workflow_contracts.py`
- `tests/test_execution_registry.py`
- `tests/test_model_program.py`
- `tests/test_tool_result_normalizer.py`
- `tests/test_execution_adapter.py`
- `docs/orchestrated/vibecad-agent-core-phase1.md`

### 7. Expected Impact

- Adds a stable, provider-neutral Agent execution contract layer.
- Makes malformed or unsafe model programs rejectable before FreeCAD is
  touched.
- Gives later Claude, Codex, skill, MCP-sampling, and BYOK adapters a shared
  protocol instead of duplicating CAD orchestration logic.
- Keeps the current VibeCAD MCP API compatible and adds no model cost or key
  handling.
- Establishes a real FreeCAD proof without turning generated Python into the
  primary execution path.

### 8. Residuals

| ID | Residual | Disposition | Closure criterion |
|---|---|---|---|
| `R-B01` | No public workflow MCP tools yet | planned next stage | separately approved workflow API stage |
| `R-B02` | No reasoning-owner adapters yet | planned after stable contracts | adapter design and threat model approved |
| `R-B03` | No transaction/checkpoint/rollback coordinator yet | planned execution stage | deterministic rollback tests pass |
| `R-B04` | No photo/video-to-mesh, mesh-to-CAD, or simulation providers | architectural extension points only | provider contracts approved later |
| `R-B05` | Contract parsing has no total node-count, serialized-byte, or string-length cap | defer to external ingress/Task Service where request-wide budgets can be enforced | approved ingress limits with adversarial wide-payload tests |
| `R-B06` | The architecture's illustrative `ModelProgram.acceptance` array differs from C1's normative nested, versioned `AcceptanceSpec` mapping | record C1 interpretation; do not edit B3 architecture outside the C1 allowlist | user reviews the representation and the public Agent API documentation is aligned before exposure |
| `R-B05` | Host has no worker model selector | accepted performance limitation | host exposes a live selector or routing is no longer needed |
| `R-B06` | No PR creation in this campaign | intentional | user separately authorizes publication |

---

## Approval and Authorization History

| Record | Timestamp | Revision | Actor | Exact authorization | Effect |
|---|---|---|---|---|---|
| `A-PENDING` | 2026-07-16 | `R1` | user | pending | no implementation, branch creation, test execution, commit, or push authorized yet |
| `A-001` | 2026-07-16T01:48:00-07:00 | `R1` | user | `**批准 R1，按计划开工。**` | authorizes the five enumerated R1 actions and boundaries below |

Approval of R1 authorizes only:

1. creation of `codex/agent-core-phase1` from the current repository anchor while
   preserving the working tree;
2. Stage A and Stage B named-file changes and gates;
3. bounded implementer/reviewer delegation described above;
4. named commits and immediate non-force pushes to that branch;
5. updates to this evidence ledger.

It does not authorize a pull request, merge, release, package publication,
external message, credential use, paid model invocation, or direct mutation of
`main`.

## Execution Ledger

No implementation evidence exists for R1 yet.

- `2026-07-16T01:48:00-07:00` — R1 approved by `A-001` at source anchor
  `9af2714c799bb34ca59907514c15cee0db7645f7`.
- `2026-07-16` — Created local branch `codex/agent-core-phase1` from the approved
  anchor while preserving the complete working tree. No commit or push yet.
- `B1 / environment evidence` — Bare `uv run pytest` first failed before
  collection because the sandbox denied `~/.cache/uv`; the approved escalated
  retry then collected 0 tests because Python 3.13.14 skipped Hatch's hidden
  `_editable_impl_vibecad.pth`. `uv sync --dev` confirmed the editable package
  metadata but did not change that interpreter behavior. B1 gates therefore use
  explicit `PYTHONPATH=src`; the host compatibility gap remains a residual.
- `B1 / gate evidence` — Focused CAD/session/server tests: 112 passed, 10
  deselected. Focused runtime/installer/manifest tests: 75 passed, 6 deselected.
  Normal suite: 485 passed, 80 deselected. Ruff: all checks passed.
- `B1 / FreeCAD evidence` — Existing managed runtime at
  `~/Library/Application Support/VibeCAD/mamba/envs/vibecad` reported FreeCAD
  1.1.0 and current 0.4.0 receipt; `runtime_ready`, `health_check`,
  `engine_compatible`, and `verify_runtime` all returned true.
- `B1 / slow RED-1` — The default slow fixture began creating an isolated
  `.vibecad-test-runtime`; this redundant run was interrupted after observable
  process inspection. Rerun with `VIBECAD_FREECAD_ENV` reused the verified
  runtime and produced 79 passed / 1 failed. Failure: the override-aware smoke
  test called managed-only `paths.env_python()`.
- `B1 / slow RED-2` — After changing the health probe to
  `active_runtime_python()`, focused retry reached a second managed-only call in
  the same test and failed before the subprocess launch.
- `B1-RP1 / bounded replan` — The two-attempt repair budget was reached. Without
  changing scope or architecture, the packet is narrowed to resolve
  `active_runtime_python()` once inside `test_install_and_smoke`, reuse it for
  both health and subprocess checks, then rerun the focused and complete slow
  gates. No production file or decision changed.
- `B1-RP1 / GREEN` — Focused override-aware smoke: 1 passed. Complete real
  FreeCAD suite using the existing verified runtime: 80 passed, 485 deselected
  in 700.77 seconds.
- `B1 / review finding F-01` — Independent audit found that an external
  `VIBECAD_FREECAD_ENV` with a legacy/server-mismatch receipt was reported as an
  automatic lightweight upgrade even though the installer intentionally never
  modifies user-owned environments. Genuine RED: 2 assertion failures. Fix:
  external mismatches now return `repair_required`. GREEN: 2 focused tests, 47
  status/installer/server tests, 487 normal tests, and Ruff all passed.
- `B1 / independent review` — Verdict `READY WITH REPAIRS`; no secret,
  generated artifact, or out-of-scope content found. The reviewer accounted for
  every B1 file and found three reproducible Session defects plus one checkpoint
  persistence gap. B1 remains on hold.
- `B1-RP2 / bounded replan` — Within the existing Session/test allowlist, add
  failure-injection coverage and repair: document replacement/close failure
  atomicity; transaction body/claim/commit Python-state rollback; rejection of
  explicit part namespaces in single-part mode; and checkpoint persistence of
  active part/result roots. Concurrency ownership, hostile/large FCStd
  isolation, arbitrary external `App::Part` interpretation, and empty-history
  redesign are explicitly not broadened into this repair.
- `B1-RP2 / implementation evidence` — Transaction failure now restores labels,
  part membership, result roots, active part, and revision while preserving the
  original exception. Document replacement and close publish new Python state
  only after close succeeds; candidate cleanup is best effort. Single-part mode
  rejects explicit `part`. Checkpoint writes current VibeCAD state before the
  FCStd copy.
- `B1-RP2 / ownership probe` — A real FreeCAD 1.1 subprocess returned true for
  `FreeCAD.getDocument(d.Name) is d`. The close helper now verifies that identity
  before closing a globally named document; affected real close/load tests pass.
- `B1-RP2 / GREEN` — 84 focused lifecycle/session/measure/project/naming tests
  passed with 4 slow tests deselected; 4 affected real-FreeCAD slow tests passed
  with 64 deselected; complete normal suite passed 491 tests with 80 deselected;
  full Ruff passed. Independent post-fix verdict is pending.
- `B1-RP2 / cleanup RED→GREEN` — Fault injection proved candidate cleanup could
  mask the primary FCStd load error. Cleanup is now best effort; the primary
  error is preserved. Focused regression: 1 passed.
- `B1 / final gates` — Complete normal suite on the final B1 tree: 492 passed,
  80 deselected in 10.01 seconds. Complete Ruff: passed. Earlier complete real
  FreeCAD suite: 80 passed; after Session changes, all 4 affected real-FreeCAD
  slow tests passed.
- `B1 / final independent review` — Verdict `ACCEPT`; no unresolved P0–P2,
  scope violation, secret, binary/generated artifact, or required B2/B3 runtime
  dependency. Non-blocking engine/concurrency residuals are recorded as
  `R-A06`–`R-A10`.
- `B1 / commit and push` — Commit
  `cb2301e6daca9ab8188fe6114f1fc29e5f485baa`
  (`feat: complete the VibeCAD 0.4.0 runtime and CAD baseline`) created from 31
  exact named files and pushed successfully to
  `origin/codex/agent-core-phase1`. Remote redirect warning is `R-A11`; no
  force-push, `main` mutation, PR, or release occurred.

For every accepted commit append:

- task packet ID and implementer;
- starting and ending commit hashes;
- exact staged files and staged-diff summary;
- RED evidence where behavior changed;
- focused, full, lint, integration, and manual-validation evidence as applicable;
- independent reviewer identity and verdict;
- pushed branch/commit confirmation;
- decision and residual updates.

### Task Packet `B1`

1. **Authorization:** Revision R1 and decisions `D-A01`–`D-A06` are
   explicitly approved by record `A-001`. This packet inherits all
   higher-priority system, developer, and user instructions, applicable
   directory-scoped `AGENTS.md`/`CLAUDE.md`, the R1 file allowlist, and the
   current host permission model and sandbox. The Skill, approved artifact, and
   this packet cannot grant or expand permissions, elevate authority, or bypass
   that model or sandbox. Do not request the same approval again.
2. **Workspace anchor:** Repository root
   `/Users/wangtao/Documents/DevProject/vibecad`; approved source anchor
   `main@9af2714c799bb34ca59907514c15cee0db7645f7`; execution branch
   `codex/agent-core-phase1`. No directory-scoped `AGENTS.md` or `CLAUDE.md` was
   observed. Modify only the exact `B1` named files. `B2`, `B3`, Stage B files,
   external services, `main`, releases, and PRs are prohibited. The current host
   permission model and sandbox remain binding.
3. **Context:** Audit and stabilize the already-present interdependent VibeCAD
   `0.4.0` runtime/CAD baseline. Success means every hunk is accounted for,
   version and manifest state agree, focused and full tests pass, lint passes,
   real installed-FreeCAD integration passes, no unrelated or secret-bearing
   content is present, and the named scope is ready for one reproducible commit.
   Known failure modes are hidden dependency on unstaged release/docs work,
   stale manifest/version references, environment-only integration failures,
   and accidental inclusion of caches or local runtime artifacts.
4. **Steps and gates:** Verify anchor and inventory; inspect the complete `B1`
   diff and classify every change; run focused tests; run the normal full suite
   and lint; run opted-in real FreeCAD integration through a live process
   session; repair only reproducible failures inside the allowlist and budget;
   inspect the exact staged diff. A distinct reviewer then evaluates scope,
   safety, contracts, and evidence. The controller alone accepts review, stages
   named files, commits, pushes, and updates the ledger.
5. **Execution discipline:** Delegation profile `spawn-send-wait`; requested
   model tier `standard` (the host exposes no selector, so no tier claim is
   made); process profile `native-session-poll`. Maximum two gate attempts.
   Stop on any out-of-allowlist write, secret-like material, unexplained hunk,
   invalid intermediate state, unrepairable test/integration failure, force-push
   requirement, or direct-`main` mutation.
6. **Delivery boundary:** The implementer/auditor may inspect, test, and make the
   smallest necessary repair within `B1`; it must return a bounded handoff and
   must not stage, commit, push, edit the campaign artifact, or start `B2`. The
   independent reviewer may not repair the implementation. Review acceptance,
   artifact updates, staging, commit, and push are reserved for the controller.
7. **Final report:** Return outcomes; start/end hashes; exact files inspected or
   changed; diff accounting; numeric focused/full/lint/integration results;
   commands and exit statuses; justified deviations; detected risks and
   residuals; and final branch/workspace state.

### Task Packet `B2`

1. **Authorization:** Revision R1, record `A-001`, commit plan `B2`, and decisions
   `D-A01`–`D-A06` are approved. This packet inherits all higher-priority
   system, developer, and user instructions, applicable directory-scoped
   `AGENTS.md`/`CLAUDE.md`, the R1 allowlist, and the current host permission
   model and sandbox. The Skill, artifact, and packet cannot expand permissions,
   elevate authority, or bypass the sandbox. Do not request the same approval.
2. **Workspace anchor:** Repository root
   `/Users/wangtao/Documents/DevProject/vibecad`; branch
   `codex/agent-core-phase1`; anchor
   `cb2301e6daca9ab8188fe6114f1fc29e5f485baa`. No directory-scoped
   `AGENTS.md`/`CLAUDE.md` was observed. Modify only
   `.github/workflows/release.yml`, `.github/scripts/check_release_versions.py`,
   `tests/test_release_workflow.py`, and this campaign artifact. B3, Stage B,
   `main`, PRs, releases, secrets, and external publication are prohibited. The
   current host permission model and sandbox remain binding.
3. **Context:** Normalize the existing release workflow change so a release tag
   cannot publish a package whose tag, `pyproject.toml`, package version, or
   MCPB manifest version disagree. Success means the checker has one explicit
   version source contract, the workflow invokes it before build/publication,
   workflow permissions remain least privilege, untrusted tag text is not
   executed, and tests cover matching/mismatching versions without network or
   publication.
4. **Steps and gates:** Inspect the complete B2 diff and workflow permissions;
   read the checker and tests completely; run focused release/workflow and
   manifest/version tests; execute the checker locally for a matching version
   and a controlled mismatch if the test does not already prove it; validate
   YAML parse and staged diff. Repair only within scope and budget. A distinct
   reviewer evaluates trigger safety, command injection, permissions, version
   sources, and test evidence. Controller alone accepts review, updates the
   artifact, stages exact files, commits, and pushes.
5. **Execution discipline:** `spawn-send-wait`; requested standard implementation
   and deep review tiers, but the host exposes no model selector so no tier claim
   is made; `native-session-poll`. Maximum two gate attempts. Stop on a secret,
   write outside allowlist, unsafe release trigger/permission, network publish,
   need to modify B1/B3, failing focused gate outside repair budget, force-push,
   or direct `main` mutation.
6. **Delivery boundary:** Implementer/auditor may inspect, test, and make the
   smallest B2 repair, but must not stage, commit, push, edit the campaign
   artifact, start B3, or trigger a release. Reviewer is read-only. Acceptance,
   artifact update, staging, commit, and push are reserved for the controller.
7. **Final report:** Return start/end hashes; exact files inspected/changed;
   workflow/checker behavior; numeric gate results; security review; staged
   scope; justified deviations; residuals; and final workspace state.

### B2 Evidence

- `B2 / permission RED→GREEN` — Added a failing contract for explicit least
  privilege. Workflow now defaults to `contents: read`; PyPI explicitly has
  `contents: read` plus `id-token: write`; MCPB alone has `contents: write`.
- `B2 / credential RED→GREEN` — Added a failing contract proving the MCPB
  checkout persisted its write-capable credential before network-executed
  `npx`. MCPB checkout now sets `persist-credentials: false`; `GH_TOKEN` remains
  scoped to the final release step.
- `B2 / gates` — Release/version and manifest suite: 16 passed. Current
  repository checker: exit 0 with all four sources at 0.4.0. YAML parse: exit 0.
  B2 Ruff and `git diff --check`: passed. No workflow, publication, release, or
  network package execution was triggered.
- `B2 / independent review` — Verdict `ACCEPT`; no unresolved P0–P2. Mutable
  action/runtime package acquisition and same-job PyPI OIDC exposure are
  recorded as `R-A12` and `R-A13`.
- `B2 / commit and push` — Commit
  `a0de03fba86b42cb595478502fdf3c74f2827eb0`
  (`ci: verify release version consistency`) created from the four exact B2
  files and pushed successfully to `origin/codex/agent-core-phase1`. No release,
  PR, force-push, or `main` mutation occurred.

### Task Packet `B3`

1. **Authorization:** Revision R1, `A-001`, commit plan `B3`, and decisions
   `D-A01`–`D-A06` are approved. This packet inherits all higher-priority
   system, developer, and user instructions, applicable directory-scoped
   `AGENTS.md`/`CLAUDE.md`, the R1 allowlist, and the host permission model and
   sandbox. The Skill, artifact, and packet cannot expand permissions, elevate
   authority, or bypass the sandbox. Do not request the same approval.
2. **Workspace anchor:** Repository root
   `/Users/wangtao/Documents/DevProject/vibecad`; branch
   `codex/agent-core-phase1`; anchor
   `a0de03fba86b42cb595478502fdf3c74f2827eb0`. No directory-scoped
   `AGENTS.md`/`CLAUDE.md` was observed. Modify only `PRIVACY.md`, `README.md`,
   `docs/ACCEPTANCE_TESTS.md`, `docs/AGENT_ARCHITECTURE.md`,
   `docs/ARCHITECTURE.md`, `docs/USER_GUIDE.md`, the two named superseded
   July-02 Agent documents, and this campaign artifact. Code, tests, B1/B2,
   Stage B, `main`, PRs, releases, and external publication are prohibited. The
   current host permission model and sandbox remain binding.
3. **Context:** Close the existing 0.4.0 documentation baseline. Current-state
   docs must match committed tools and FreeCAD behavior. Target-state Agent
   architecture must be clearly labeled unimplemented, canonical, provider- and
   model-neutral, BYOK/external-plan oriented, and consistent with the user's
   accepted positioning. Old Claude-bound Agent documents must remain available
   for history but be clearly superseded. Privacy claims must describe only
   currently shipped network/data behavior and must not pre-authorize future
   model transfer.
4. **Steps and gates:** Inspect every B3 diff and read every new document fully;
   cross-check tool count/names/version/current limitations against B1 code and
   tests; validate links and supersession headers; search for stale model/token,
   managed-model, implemented-Agent, and contradictory privacy claims; repair
   only within scope. A distinct reviewer evaluates current-vs-target truth,
   security/privacy language, roadmap boundaries, and link integrity. Controller
   accepts review, updates the artifact, stages exact files, commits, and pushes.
5. **Execution discipline:** `spawn-send-wait`; requested standard documentation
   and deep architecture review tiers, but no host selector exists so no tier
   claim is made; `native-session-poll`. Maximum two gate attempts. Stop on
   claims unsupported by committed code, privacy expansion requiring user
   policy, missing canonical decision, out-of-allowlist write, secret/personal
   data beyond existing public project metadata, force-push, or `main` mutation.
6. **Delivery boundary:** Implementer/auditor may inspect and make the smallest
   documentation repair within B3 but may not stage, commit, push, edit the
   campaign artifact, start Stage B, publish, or open a PR. Reviewer is
   read-only. Controller owns acceptance, ledger, staging, commit, and push.
7. **Final report:** Return hashes; exact files; current/target claim matrix;
   link and stale-claim checks; privacy assessment; changes; deviations;
   residuals; reviewer verdict; and final workspace state.

### B3 Evidence

- `B3 / current-state truth` — Cross-checked the committed manifest and B1
  implementation: version 0.4.0, 31 tool declarations, and 31 unique names.
  Current documentation describes the in-process FreeCAD 1.1.0 service and
  explicitly states that TaskRun, ModelProgram execution, internal planning,
  Sampling, BYOK, and Agent eval are not shipped in 0.4.0.
- `B3 / target-state boundary` — `docs/AGENT_ARCHITECTURE.md` is the Accepted
  target decision source. Both July-02 Agent documents are prominently marked
  Superseded and route readers to it. External Plan, optional future Sampling,
  and user BYOK remain target modes; no managed VibeCAD model or token product
  is claimed or implemented.
- `B3 / privacy repair` — Replaced absolute “never transmitted” language with
  the precise boundary: the VibeCAD CAD backend does not independently upload
  design files or operate telemetry/cloud storage, while MCP tool requests and
  results pass to the user-selected client and are subject to that client and
  model provider's policy. The policy explicitly records that 0.4.0 has no
  direct provider, Sampling, or BYOK call path; this does not pre-authorize a
  future transfer.
- `B3 / stale-claim repair` — Updated the release architecture debt to reflect
  B2's version guard on both publication jobs, while preserving the real full-CI
  and supply-chain residuals. Qualified the user-guide upgrade promise for
  incompatible or damaged Python/FreeCAD environments.
- `B3 / gates` — All 11 relative Markdown targets checked in the B3 set resolve;
  stale 0.3.0/23-tool/current-Agent/privacy/release claims search has no invalid
  hit (the remaining 0.3.0 reference is the deliberate upgrade fixture);
  `git diff --check` passed.
- `B3 / independent review` — Verdict `ACCEPT`; no unresolved P0–P2. The
  reviewer inspected all eight product/architecture documents plus committed
  manifest/code/test facts and made no write, stage, commit, or push.
- `B3 / commit and push` — Commit
  `f2e60875b4c8fb6944cad1c0c75abe518015be82`
  (`docs: document VibeCAD 0.4.0 and the target agent architecture`) contains
  the nine exact B3 files and is pushed to
  `origin/codex/agent-core-phase1`. HEAD and upstream matched and the worktree
  was clean before Stage B ledger work began. `R-A01` is closed.

### Task Packet `C1`

1. **Authorization:** Revision R1, `A-001`, Stage B decisions `D-B01`–`D-B08`,
   and commit plan `C1` are approved. This packet inherits all higher-priority
   system, developer, and user instructions, applicable directory-scoped
   repository instructions, and the host sandbox. It cannot expand permissions
   or bypass any circuit breaker. Routine implementation judgments stay inside
   the accepted contract boundary; product or architecture choices are logged
   for the user rather than decided by the worker.
2. **Workspace anchor:** Repository root
   `/Users/wangtao/Documents/DevProject/vibecad`; branch
   `codex/agent-core-phase1`; clean pushed anchor
   `f2e60875b4c8fb6944cad1c0c75abe518015be82`. Modify only
   `src/vibecad/workflow/__init__.py`,
   `src/vibecad/workflow/contracts.py`,
   `src/vibecad/workflow/errors.py`, `tests/test_workflow_contracts.py`, and
   this campaign artifact. Existing server, tools, runtime, engine, manifest,
   dependency metadata, other tests/docs, PRs, releases, and `main` are out of
   scope.
3. **Context:** Establish the first provider-neutral, pure-standard-library
   workflow boundary. Define typed `schema_version=1` representations for
   intent, acceptance criteria, model commands/programs, execution evidence,
   normalized errors, and outcomes. Objects must round-trip through plain
   mappings without FreeCAD, MCP, model SDKs, keys, network, filesystem side
   effects, or arbitrary code. Supported values fail closed on booleans used as
   integers, unknown fields, unsupported versions/enums, malformed collections,
   and missing/blank identifiers. This commit defines data shape only; it does
   not validate dependency graphs or execute programs.
4. **Steps and gates:** Inspect package/test conventions; add a focused test
   that fails for missing modules/behavior and record genuine RED before
   implementation. Implement the smallest immutable dataclass/enums and stable
   structured contract error needed for exact mapping round-trip and strict
   parsing. Cover every contract's valid round-trip plus unsupported version,
   invalid enum, unknown/missing field, malformed nested item, and import
   boundary failures. Run the focused suite with `PYTHONPATH=src`, a clean
   interpreter import assertion that no `FreeCAD`, `Part`, `mcp`, or model SDK
   module was imported, Ruff on exact files, and `git diff --check`. A distinct
   read-only reviewer evaluates schema stability, type traps, compatibility,
   security, and scope. The controller alone accepts review, updates the
   artifact, stages named files, commits, and pushes.
5. **Execution discipline:** `spawn-send-wait`; the host has no model-tier
   selector, so no standard/deep tier claim is made; `native-session-poll`.
   Maximum two gate/repair attempts. Stop on an out-of-allowlist write, need for
   a dependency/framework, schema/persisted behavior not settled by R1, import
   of FreeCAD/MCP/model code, credential/network/file I/O, arbitrary-code field,
   existing public API change, or failing required gate outside repair budget.
6. **Delivery boundary:** The bounded implementer may inspect, add the genuine
   RED test, implement only the four C1 code/test files, and run focused gates.
   It must not edit this artifact, stage, commit, push, begin C2, or alter
   existing product files. The distinct reviewer is read-only. Acceptance,
   ledger updates, named staging, commit, and push are reserved for the
   controller.
7. **Final report:** Return anchor/end hashes; exact files changed; RED command
   and failure reason; contract inventory and strictness rules; focused/import/
   lint/diff gate outputs; reviewer findings and verdict; deviations/residuals;
   staged scope; commit/push status; and final workspace state.

### C1 Evidence

- `C1 / genuine RED` — After the first focused fixture was added,
  `UV_CACHE_DIR=/tmp/vibecad-c1-uv-cache PYTHONPATH=src uv run pytest -q
  tests/test_workflow_contracts.py` exited 2 with
  `ModuleNotFoundError: No module named 'vibecad.workflow'`. An earlier default
  `uv` attempt hit the already-recorded sandbox/cache environment issue and is
  not counted as behavioral RED.
- `C1 / contract inventory` — Added pure-standard-library, frozen
  `schema_version=1` contracts for `IntentAssumption`, `Intent`,
  `AcceptanceCriterion`, `AcceptanceSpec`, `ModelCommand`, `ModelProgram`,
  `ExecutionEvidence`, `StepError`, and `StepResult`; typed enums cover task,
  acceptance, value-source, evidence, and accepted error taxonomies.
  `ContractValidationError` is itself a strict, versioned, round-trippable
  mapping with stable code and RFC 6901 input pointer.
- `C1 / strict boundary` — Parsers reject missing/unknown fields, unsupported
  versions/enums, blank identifiers/text, malformed nested collections,
  booleans used as numbers, non-finite/negative elapsed values, non-JSON data,
  signed integers outside IEEE-754's interoperable safe range, container depth
  above 64, and cyclic JSON. Caller-owned payloads are copied and deeply frozen;
  output contains only dict/list/scalars. Shared sibling aliases remain valid.
  Pointer escaping is collision-free for `~`, `/`, indices, and controls;
  exception rendering quotes path/message to prevent log-line injection.
- `C1 / C4-ready result shape` — `StepResult` requires a generic deeply frozen
  `value` and measured `elapsed_ms`. `StepError` requires explicit retryability,
  user-input policy, related objects, and diagnostic artifacts. This prevents
  C4 from inventing undocumented wrappers or adding unknown v1 fields later.
- `C1 / repair RED→GREEN` — The independent review first rejected missing
  result/error/version/path fields. The concentrated adversarial test set was
  RED at collection because `errors.py` did not yet expose the shared schema
  constant, then passed after implementation. A final probe produced
  `1 failed, 74 passed` by showing that a 5,001-digit `schema_version` leaked
  Python's integer-rendering error; safe-range validation was moved before all
  interpolation. Final focused result: `75 passed`.
- `C1 / gates` — Exact-file Ruff lint passed; Ruff format reports all four files
  formatted; `git diff --check` passed. The managed runtime Python 3.12.13 at
  `/Users/wangtao/Library/Application Support/VibeCAD/mamba/envs/vibecad/bin/python`
  imported the checkout's workflow package without loading FreeCAD, Part, MCP,
  Anthropic, OpenAI, Cohere, or Mistral modules.
- `C1 / independent review` — Initial verdict `REJECT` identified one P1 and
  five P2 schema/security gaps. After the concentrated repair and huge-version
  regression repair, final verdict `ACCEPT`; no unresolved P0–P2. The reviewer
  made no edit, stage, commit, or push.
- `C1 / interpretations and residuals` — A nested, versioned
  `AcceptanceSpec` is normative for C1; the raw acceptance array in the
  architecture example is treated as illustrative. Total node/byte/string
  limits remain deferred to the external ingress budget. These are recorded as
  `R-B06` and `R-B05` for user review rather than silently expanded here.
- `C1 / commit and push` — Commit
  `0fb87eabb3761b8e0859bf9d9402956490ce2886`
  (`feat(workflow): define versioned agent contracts`) contains the five exact
  C1 files and is pushed to `origin/codex/agent-core-phase1`. HEAD and upstream
  matched and the worktree was clean before the C2 ledger update.

### Task Packet `C2`

1. **Authorization:** Revision R1, `A-001`, Stage B decisions `D-B01`–`D-B08`,
   and commit plan `C2` are approved. This packet inherits all higher-priority
   instructions, repository-local instructions, and sandbox restrictions. It
   cannot expand permissions. Routine implementation choices stay inside the
   narrow Phase-1 registry; adding product coverage or a code-execution path is
   not authorized.
2. **Workspace anchor:** Repository root
   `/Users/wangtao/Documents/DevProject/vibecad`; branch
   `codex/agent-core-phase1`; clean pushed anchor
   `0fb87eabb3761b8e0859bf9d9402956490ce2886`. Modify only
   `src/vibecad/execution/__init__.py`,
   `src/vibecad/execution/registry.py`,
   `tests/test_execution_registry.py`, and this campaign artifact. Existing
   workflow contracts, server, tools, runtime, engine, manifest, dependency
   metadata, other tests/docs, PRs, releases, and `main` are out of scope.
3. **Context:** Add an immutable, pure-standard-library metadata registry, not
   an executor. The default Phase-1 slice contains exactly four provider-neutral
   operations needed by C3/C5: `create_document` → `new_document`, `create_box`
   → `add_box`, `modify_parameter` → `modify_part`, and `inspect_model` →
   `describe_part`. Metadata describes program target fields, argument fields,
   handler-parameter bindings, value shape, maximum risk class, and whether
   deterministic evidence is required. `modify_parameter.target.object` binds
   to the existing tool's `name` parameter; no arbitrary callable, import path,
   Python source, shell command, provider, credential, network, filesystem
   action, or CAD side effect belongs in the registry. This is deliberately not
   a claim that all 31 tools are Agent-ready.
4. **Steps and gates:** Inspect existing tool signatures and C1 vocabulary; add
   a focused test that fails because the execution registry does not exist and
   record genuine RED. Implement the smallest frozen field/operation metadata,
   risk/value-shape enums, stable machine-readable registry errors, immutable
   registry lookup, and the four-entry default registry. Validate snake-case
   names, nonblank/distinct target/argument/tool-parameter fields, duplicate
   operations/fields/bindings, exact unsafe code/script/shell operation tokens,
   and unknown lookup. Required/optional field metadata must be sufficient for
   C3 to reject missing, extra, and wrong-shape values without CAD access.
   Tests cover exact default mappings/risk/evidence flags, immutability,
   duplicate and arbitrary-code rejection, unknown lookup, and a clean import
   boundary. Run focused tests with `PYTHONPATH=src`, managed Python 3.12 import
   proof, exact-file Ruff lint/format, and `git diff --check`. A distinct
   read-only reviewer evaluates registry safety, metadata completeness, future
   C3/C5 usability, and scope. Controller alone accepts, updates the ledger,
   stages named files, commits, and pushes.
5. **Execution discipline:** `spawn-send-wait`; no model-tier selector is
   available, so no tier claim is made; `native-session-poll`. Maximum two
   gate/repair attempts. Stop on out-of-allowlist writes, callable/dynamic
   import/source/command fields, need to import existing tool/server/FreeCAD/MCP
   or a model SDK, unclear operation semantics outside the four-entry slice,
   public API changes, or a required gate failure outside repair budget.
6. **Delivery boundary:** The bounded implementer may inspect, add genuine RED,
   implement only the three C2 code/test files, and run focused gates. It must
   not edit this artifact, stage, commit, push, begin C3, or alter existing
   product code. The independent reviewer is read-only. Acceptance, ledger
   updates, staging, commit, and push remain controller-only.
7. **Final report:** Return anchor/end hashes; exact files; RED command/failure;
   registry/metadata inventory and four operation mappings; rejection and
   immutability rules; focused/import/lint/diff results; reviewer findings;
   deviations/residuals; staged scope; commit/push status; final workspace.

## Recovery Snapshot

### 1. Completed work

- Target Agent architecture accepted in `docs/AGENT_ARCHITECTURE.md`.
- Orchestrated campaign revision R1 drafted.
- Host capability profile and adapter evidence recorded.
- B1 runtime/CAD baseline committed and pushed as `cb2301e6` after complete
  normal/FreeCAD gates and independent review.

### 2. Current state

- Approval: R1 approved by `A-001`.
- Active stage: Stage B / packet `C1` accepted and ready to commit.
- Branch: `codex/agent-core-phase1`.
- Anchor: `f2e60875b4c8fb6944cad1c0c75abe518015be82`.
- Working tree: dirty only with the five exact C1 code/test/ledger paths.
- Implementation/delegation/gates: Stage A complete; C1 implementation and
  independent review accepted with all required gates green.
- Commits/pushes: B1, B2, and B3 complete and pushed.

### 3. Next actions

1. Stage exact C1 files plus ledger, inspect the staged diff, commit, and push.
2. Confirm clean/upstream-equal state.
3. Issue the seven-section C2 packet before registry implementation.

### 4. Blockers and residuals

- `R-A01` is closed at the Stage B anchor.
- No authorization blocker remains for R1; all scope and circuit breakers stay
  binding.

---

## C2 Attempt Evidence and Circuit Breaker

- `C2 / genuine RED` — With the first focused fixture present,
  `UV_CACHE_DIR=/tmp/vibecad-c2-uv-cache PYTHONPATH=src uv run pytest -q
  tests/test_execution_registry.py` exited 2 during collection with
  `ModuleNotFoundError: No module named 'vibecad.execution'`.
- `C2 / initial implementation` — Added the three exact C2 files and the four
  default mappings. Focused tests first reached 27 passed. Controller pre-review
  found that `discard_unsaved` exposed a destructive guard bypass, registry
  errors were not versioned, unsafe-name tokens overblocked legitimate semantic
  file/path/source fields, and adversarial lookup coverage was incomplete.
- `C2 / repair attempt 1` — Removed `discard_unsaved`; retained
  `create_document` maximum risk `destructive`; added strict schema-v1 registry
  error round-trip and bounded lookup errors; narrowed unsafe tokens to actual
  execution primitives. Focused tests reached 44 passed and C1+C2 reached 119
  passed. The independent reviewer then found two P2 gaps: obvious execution
  aliases such as `run_bash`, `run_freecad_macro`, and `spawn_process` were
  accepted, and hostile Mapping/Iterable implementations could leak ordinary
  runtime exceptions from public constructors/parsers.
- `C2 / repair attempt 2` — Added exact cross-platform interpreter, shell,
  macro, spawn, and fork token rejection without blocking legitimate
  file/path/source/process or substring-only CAD names. Normalized ordinary
  `Exception` from hostile Mapping/Iterable access to bounded `RegistryError`,
  preserved existing structured errors, and proved that `BaseException` is not
  caught. Final controller gates at this attempt: C2 61 passed; C1+C2 136
  passed; managed Python 3.12.13 clean import passed without FreeCAD, Part, MCP,
  or model SDKs; exact Ruff lint and format passed; tracked and new-file
  whitespace checks emitted no diagnostics.
- `C2 / circuit breaker C2-BRK-01` — The independent read-only re-review proved
  that `RegistryError` accepts DEL/C1 controls and Unicode line separators in
  messages while rendering them raw. For example, `bad\u2028forged` is accepted
  and splits the public exception string into two log lines. This is an
  unresolved P2 log-forging boundary defect. Because packet C2 permits at most
  two repair/gate attempts, execution stopped without staging, commit, push, or
  C3 work. HEAD remains
  `0fb87eabb3761b8e0859bf9d9402956490ce2886`.
- `C2 / scope and preserved result` — The worktree contains only the
  controller-owned campaign artifact plus the three expected C2 files. The
  exact four operation mappings, frozen metadata, handler bindings, risk and
  evidence flags, `discard_unsaved` exclusion, pure import boundary, and prior
  P2 repairs remain locally present but are not accepted or committed while
  `C2-BRK-01` is open.

### Residual Corrections and Additions

- `R-B07` supersedes the second duplicate `R-B05` row: the host exposes no
  worker model selector; this remains an accepted performance limitation.
- `R-B08` supersedes the second duplicate `R-B06` row: this campaign does not
  authorize PR creation; user authorization remains the closure condition.
- `R-B09` — Registry error rendering permits non-C0 line-breaking/control
  characters and can forge multi-line logs. Disposition: blocking C2. Closure
  requires a focused RED for DEL, U+0085, U+2028, and U+2029; one bounded
  implementation that either rejects non-printable/line-breaking characters or
  safely quotes rendering at both constructor and strict-parser boundaries;
  final C2/C1 compatibility, managed-import, Ruff/format/diff gates; and
  independent read-only acceptance.

## Proposed Revision R2 — Not Authorized

R2 changes no product architecture, file allowlist, commit count, external
authority, or C2 semantic scope. It proposes exactly one additional concentrated
repair pass to close `R-B09` in
`src/vibecad/execution/registry.py` and
`tests/test_execution_registry.py`, with this campaign artifact updated only
for evidence. All R1 prohibitions remain: no C3 start before accepted C2, no
existing product-file edit, no PR, merge, release, provider/model/key/network
work, `main` mutation, force-push, or commit beyond the existing campaign
budget.

Required authorization wording: `批准 R2，关闭 R-B09 后继续 R1。`

## Recovery Snapshot `S-C2-BLOCKED-01`

### 1. Completed milestones

- Repository `/Users/wangtao/Documents/DevProject/vibecad`, branch
  `codex/agent-core-phase1`, verified pushed anchor
  `0fb87eabb3761b8e0859bf9d9402956490ce2886`.
- B1 `cb2301e6`, B2 `a0de03f`, B3 `f2e6087`, and C1 `0fb87ea` are committed and
  pushed. C1 focused gate is 75 passed and its independent verdict is ACCEPT.
- C2 has genuine RED and three observable implementation states preserved in
  the working tree/ledger. No C2 commit exists.

### 2. Ordered next packets and branch conditions

1. If the user explicitly approves proposed R2, issue one concentrated
   `C2-RP1` packet limited to `R-B09`, run the named gates, and obtain a distinct
   final read-only verdict.
2. If final C2 review accepts with no unresolved P0-P2, update this ledger,
   stage the four exact C2 files, commit
   `feat(execution): register safe semantic CAD operations`, and immediately
   push the branch.
3. Only after HEAD and upstream match at accepted C2 may the controller append
   and issue packet C3.
4. If R2 is rejected or changed, preserve the current worktree and draft a new
   revision; do not infer authorization.

### 3. Active decisions and authorization

- R1 remains approved by `A-001`; `D-B01`–`D-B08`, the Stage B allowlist, and
  all R1 prohibitions remain active.
- `C2-BRK-01` exhausts the C2 repair budget. Proposed R2 is not authorized and
  grants no implementation authority until the user supplies the exact or
  equivalently explicit approval.

### 4. Execution discipline and recovery checks

- Capability profile remains `native-plan / spawn-send-wait / repo-artifact /
  native-session-poll`; the four-category adapter evidence record at the top of
  this artifact remains authoritative.
- On recovery, verify branch, HEAD/upstream, exact dirty-file set, and the
  136-test C1+C2 gate before any authorized repair. Preserve the named-file
  allowlist, independent reviewer boundary, exact staging, immediate push, and
  no-PR/no-main/no-release circuit breakers.

### C2 Final Review Binding

- Distinct reviewer `/root/c2_review` final verdict: `REJECT — not
  commit-ready`; no P0/P1 and exactly one unresolved P2, `R-B09`. The reviewer
  remained read-only.
- The reviewer independently confirmed that both earlier P2 findings are
  closed, all four mappings/signatures and metadata are correct,
  `discard_unsaved` is absent, the registry is immutable and sufficient for
  C3/C5, imports are pure, and no callable, dynamic import, source/command
  payload, provider credential, network/filesystem action, or CAD side effect
  was introduced.
- Reproduced control characters are DEL, U+0085, U+2028, and U+2029. Minimal
  closure is centralized printable-message validation in both direct
  construction and strict parsing, parameterized RED/GREEN coverage for all
  four characters, bounded `INVALID_ERROR_RECORD` output without the hostile
  character, the complete C2 gates, and final independent acceptance.
- Non-blocking reviewer notes: unsafe-name rejection is defense-in-depth while
  the exact default registry remains the primary allowlist; finite in-process
  Mapping iteration relies on the broader request-budget residual `R-B05`;
  Phase-1 dimensions follow the current FreeCAD millimetre convention.
- `R-B10` — The architecture example includes a `unit` argument while the
  current `modify_part` handler and C2 registry intentionally expose only
  `name/parameter/value`. C3 must reject `unit` as extra input for now. Before a
  public Agent API is exposed, the user must choose either explicit unit-aware
  v1 conversion semantics or document millimetres as the fixed v1 unit.

## R2 Authorization and Recovery

- `A-002` — At `2026-07-16T18:52:20-07:00`, the user explicitly authorized
  proposed revision R2 with: `批准 R2，关闭 R-B09 后继续 R1。`
- Effect: R2 is active and authorizes exactly one concentrated `C2-RP1` repair
  pass for `R-B09`, its named gates, distinct read-only re-review, exact C2
  staging/commit/push after acceptance, and continuation of the already
  approved R1 sequence. It does not expand the product architecture, file or
  commit budget, external authority, or any R1 prohibition.
- Recovery verification before state-changing work: branch
  `codex/agent-core-phase1`; HEAD and upstream both
  `0fb87eabb3761b8e0859bf9d9402956490ce2886`; dirty paths exactly the campaign
  artifact plus the three expected C2 paths; C1+C2 recovery gate 136 passed;
  tracked `git diff --check` passed.

### Task Packet `C2-RP1`

1. **Authorization:** R2 and `A-002` authorize one additional concentrated
   repair solely to close `R-B09`. This packet inherits R1, `A-001`,
   `D-B01`–`D-B08`, the host sandbox, repository instructions, and every
   existing prohibition. It cannot expand permissions, product behavior, or
   the semantic operation set.
2. **Workspace anchor:** Repository root
   `/Users/wangtao/Documents/DevProject/vibecad`; branch
   `codex/agent-core-phase1`; pushed anchor
   `0fb87eabb3761b8e0859bf9d9402956490ce2886`. Modify only
   `src/vibecad/execution/registry.py`,
   `tests/test_execution_registry.py`, and this campaign artifact. The
   implementer must not edit the artifact. `src/vibecad/execution/__init__.py`
   and every existing workflow/server/tool/runtime/engine/manifest file remain
   unchanged.
3. **Context:** `RegistryError` currently rejects only C0 characters with
   ordinal values below 32. DEL, U+0085, U+2028, and U+2029 pass direct
   construction and strict error-record parsing; raw exception interpolation
   can then create additional log lines. Close only this boundary while
   retaining printable Unicode messages, strict schema-v1 round-trip, bounded
   diagnostics, and all accepted C2 registry behavior.
4. **Steps and gates:** Add a parameterized focused regression for all four
   characters at both direct-constructor and `from_mapping` boundaries; run it
   first and record genuine RED matching the known log-forging behavior.
   Centralize the smallest printable/non-line-breaking message predicate and
   use it consistently in construction and parsing. Direct invalid construction
   returns a fixed safe `ValueError`; strict parsing returns bounded
   `INVALID_ERROR_RECORD`, with no hostile character in the rendered public
   error. Run focused C2 tests, C1+C2 compatibility, managed Python 3.12 clean
   import, exact-file Ruff lint/format, tracked plus new-file whitespace checks,
   and a direct split-lines probe. The same distinct reviewer re-checks `R-B09`
   and the complete C2 safety boundary read-only. Controller alone accepts,
   updates the ledger, stages exact files, commits, and pushes.
5. **Execution discipline:** `spawn-send-wait`; no worker model selector is
   available, so no tier claim is made; `native-session-poll`. This is the one
   R2 repair pass. Stop on any unexpected red, remaining P0–P2, out-of-allowlist
   write, behavior beyond message safety, caught `BaseException`, non-printable
   character reflected in a diagnostic, import/side effect, or required-gate
   failure. A further repair requires a new approval revision.
6. **Delivery boundary:** The bounded implementer may change only the two named
   code/test files and run the declared gates. It must not edit the artifact,
   stage, commit, push, begin C3, or alter the four operation mappings. The
   reviewer is strictly read-only. The controller reserves ledger updates,
   exact named staging, commit, immediate push, and the C3 transition.
7. **Final report:** Return anchor/end hashes; exact changed files; focused RED
   command and failure signal for all four characters/both boundaries; the
   centralized validation rule; focused/compatibility/import/lint/format/diff/
   split-lines results; reviewer verdict and residuals; staging/commit/push
   state; and final workspace status.

### C2-RP1 Evidence and C2 Acceptance

- `C2-RP1 / genuine RED` — After adding only the focused regression,
  `UV_CACHE_DIR=/tmp/vibecad-c2-rp1-uv-cache PYTHONPATH=src uv run pytest -q
  tests/test_execution_registry.py -k registry_error_message_boundary` exited
  1 with exactly 8 failed and 61 deselected. DEL, U+0085, U+2028, and U+2029
  were accepted at both direct construction and strict parser boundaries; the
  probe observed split-line counts 1, 2, 2, and 2 respectively.
- `C2-RP1 / implementation` — Added one `_is_safe_error_message` predicate:
  exact string, nonblank, at most 256 characters, printable, and exactly one
  rendered line. Direct invalid construction raises the fixed safe
  `ValueError` message; strict parsing raises schema-v1
  `INVALID_ERROR_RECORD` without hostile input reflection. Printable Unicode
  (`尺寸验证通过 — café ✅`) round-trips unchanged.
- `C2-RP1 / GREEN` — Focused boundary gate: 9 passed and 61 deselected. Full C2:
  70 passed. C1+C2 compatibility: 145 passed. Managed Python 3.12.13 clean
  import passed without FreeCAD, Part, MCP, Anthropic, OpenAI, Cohere, or
  Mistral modules. Exact Ruff lint and format passed; tracked and all three
  new-file whitespace checks produced no diagnostics.
- `C2-RP1 / controller probe record` — The controller's first supplemental
  one-line probe exited 1 with a Python `SyntaxError` because literal shell
  newline escapes reached `python -c`; it never imported or exercised product
  behavior and is classified as a tool-command construction failure, not
  semantic RED. The equivalent `exec`-wrapped probe then passed: four
  characters across two boundaries were rejected and every public rendering
  remained single-line.
- `C2 / final independent review` — Distinct reviewer `/root/c2_review` verdict
  `ACCEPT — C2 is commit-ready`; no unresolved P0–P2. The reviewer independently
  re-probed `R-B09`, confirmed both former P2 repairs, exact mappings/signatures,
  immutability, C3/C5 metadata sufficiency, pure imports, no side effects, and
  exact dirty scope. Review remained strictly read-only.
- `R-B09` is closed on the accepted C2 tree. `R-B05` remains the request-wide
  budget residual, the unsafe-token list remains defense-in-depth behind the
  exact default allowlist, and `R-B10` remains deferred until public Agent API
  design. No other C2 residual blocks commit.
