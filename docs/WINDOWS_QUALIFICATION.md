# Windows qualification and release gates

Status: **planned and executable, not yet a Windows support claim**.

VibeCAD currently declares the shipped application, authenticated local daemon,
task store, and managed Worker product as Darwin-only. The repository already
contains `win-64` runtime installation and compatibility code, and conda-forge
publishes a FreeCAD 1.1.0 build for Windows x86-64. Those facts are necessary,
but they do not prove the complete product boundary on Windows.

This document defines the evidence required before README, release metadata, or
runtime discovery may claim Windows support. A phase is fail-closed: a later
phase cannot compensate for a missing or failed earlier phase.

## Fixed target

- Platform identity: `windows.x86_64`.
- Local hardware: the maintainer's native dual-boot Windows installation.
- Hosted compatibility runner: `windows-2022`, never `windows-latest`.
- Managed runtime: Python 3.12 and the repository-pinned FreeCAD 1.1.0 runtime.
- Source identity: one exact, clean Git commit used by hosted and dual-boot runs.
- Public schema: unchanged unless a separately reviewed migration says otherwise.

Windows ARM is outside this qualification. A hosted VM pass is not a substitute
for the native dual-boot product gate, and a native pass is not a substitute for
the reproducible hosted contract gate.

External baselines used by this plan:

- [GitHub-hosted runner reference](https://docs.github.com/en/actions/reference/runners/github-hosted-runners)
  lists both `windows-2022` and `windows-2025` as supported x64 labels;
- [GitHub runner image catalog](https://github.com/actions/runner-images)
  documents the moving `windows-latest` alias, which is why this plan pins a
  versioned label;
- [conda-forge FreeCAD files](https://anaconda.org/conda-forge/freecad/files)
  publishes FreeCAD 1.1.0 for `win-64`, including Python 3.12 builds.

## Gate sequence

### W0 — hosted contracts

Run the `Windows qualification` workflow with `phase=contracts`. It executes on
the pinned `windows-2022` image and covers the Windows platform mapping, runtime
paths, micromamba download contract, DLL search preparation, status/installer
fallbacks, Windows file locking, process supervision, and non-slow Worker tests.

Exit criteria:

- the frozen dependency install succeeds;
- Ruff and every selected non-slow test pass;
- the evidence artifact records the exact commit and Windows host metadata;
- no test is made green by changing the product claim to a weaker boundary.

### W1 — managed runtime

Run the workflow with `phase=managed-runtime`, then repeat the same phase on the
native dual-boot installation. The gate creates a fresh, isolated
`VIBECAD_HOME`, installs the current managed runtime, imports FreeCAD through the
managed Python, fingerprints Windows/Python/FreeCAD, and runs a real Worker
round trip.

Exit criteria:

- the selected conda subdir is exactly `win-64`;
- managed Python imports FreeCAD 1.1.0 without relying on a system FreeCAD;
- Worker create/recompute/save/reopen/close succeeds headlessly;
- no FreeCAD or Worker process remains;
- the exact isolated `VIBECAD_HOME` is removed after the gate.

### W2 — Windows product security boundary

This phase is blocked until native Windows implementations and negative tests
close all of these Darwin-only assumptions:

1. authenticated local daemon transport and peer identity;
2. task/application store DACL and reparse-point handling;
3. artifact transfer without POSIX `SCM_RIGHTS` semantics;
4. process-tree termination and PID reuse protection using Windows primitives;
5. atomic checkout/staging behavior without Darwin thread-local `fchdir`;
6. Visual input/store cleanup and link/reparse rejection;
7. `windows.x86_64` reviewed-attestation resource selection and pinning.

The implementation must use Windows ACL, handle, named-pipe, Job Object, and
reparse-point semantics where applicable. POSIX mode-bit assertions must not be
presented as Windows isolation evidence.

### W3 — native reviewed product

After W2, run `phase=product` on native dual-boot Windows. The minimum product
matrix must include:

- empty project bootstrap and immutable candidate commit;
- same-run result references and reviewed operation execution;
- trusted STEP import and ImagePlane create/update;
- Sketch create/update followed by PartDesign Promotion and Groove;
- solid → authenticated FlatFace Sketch → Hole;
- content-bound PartDesign face/edge/vertex references;
- planar-mechanical Visual → Sketch/PFG → Pad/Pocket;
- save, checkpoint, reopen, duplicate rejection, late failure rollback;
- Worker crash, timeout, cancellation, process-tree cleanup, and retry isolation.

Every test must run against the managed runtime from the exact source commit.
Tests that are skipped because Windows is unsupported make W3 fail; they do not
reduce the denominator.

### W4 — reviewed attestation and release activation

Run `phase=attestation` only after W3. Add a canonical
`windows.x86_64` packaged attestation resource and a source pin selected from the
trusted runtime platform identity. Regenerate it on native Windows, then run the
generator in `--check` mode on both native Windows and the hosted workflow.

Windows becomes a release blocker only in the activation commit that also:

- makes `release.yml` depend on the Windows qualification job;
- verifies the exact wheel-installed Windows resource, not checkout-only bytes;
- keeps Intel macOS, ARM macOS, and Windows resources independently pinned;
- updates README/architecture/support metadata from Darwin-only;
- contains the reviewed cross-platform diff report;
- has a successful cleanup receipt for the native Windows test environment.

Until that atomic commit lands, Windows qualification is informative and
fail-closed; it must not be advertised as shipped support.

## Dual-boot handoff

### Before rebooting from macOS

1. Finish the integration branch and commit every tracked change.
2. Require `git status --porcelain=v1` to be empty in the qualification worktree.
3. Record `git rev-parse HEAD` as `EXPECTED_COMMIT` outside the repository.
4. Make the commit reachable from Windows through an explicitly approved remote
   push or a SHA-256-recorded Git bundle on a shared volume.
5. Stop all VibeCAD, Worker, FreeCAD, pytest, and runtime-installer processes.

Do not copy a dirty worktree between operating systems. Do not place evidence or
managed runtime files inside the repository.

### On native Windows

Use a fresh clone or clean worktree at `EXPECTED_COMMIT`, then run from an x64
PowerShell session:

```powershell
$ErrorActionPreference = "Stop"
git status --porcelain=v1
git rev-parse HEAD
uv sync --frozen
uv run --frozen ruff check .
uv run --frozen pytest -q -m "not slow"
```

Create a unique evidence directory and isolated runtime root outside the repo:

```powershell
$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$env:WINDOWS_EVIDENCE = Join-Path $env:TEMP "vibecad-windows-$stamp"
$env:VIBECAD_HOME = Join-Path $env:TEMP "vibecad-runtime-$stamp"
$env:VIBECAD_RUN_INTEGRATION = "1"
New-Item -ItemType Directory $env:WINDOWS_EVIDENCE | Out-Null
uv run --frozen python -c "from vibecad.runtime.installer import RuntimeInstaller; RuntimeInstaller().install()"
```

Then execute the W1, W3, and W4 selectors from the workflow at the same commit.
Capture JUnit XML, the exact commit, `Get-ComputerInfo`, managed FreeCAD version,
and attestation digests in `WINDOWS_EVIDENCE`.

### Before returning to macOS

1. Run the exact-runtime uninstaller and verify `VIBECAD_HOME` is absent.
2. Verify no FreeCAD, managed Python, Worker, micromamba, or pytest process remains.
3. Preserve the evidence directory and its SHA-256 manifest on the shared volume.
4. Do not commit raw host paths, usernames, environment variables, or temporary
   logs. Only canonical reviewed attestation resources and sanitized summaries
   belong in Git.

After rebooting, compare the Windows evidence commit to the final integration
commit before accepting or generating any release resource.

## Current activation blockers

The manual workflow is intentionally not in `release.yml` dependencies yet.
Activation requires W0–W4 to pass on one exact final commit. This separation
prevents a plan-only commit from either breaking Darwin releases or falsely
claiming that Windows is already supported.
