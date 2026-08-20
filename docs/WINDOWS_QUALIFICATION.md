# Windows qualification and release gates

Status: **completed for `windows.x86_64`; release-blocking gates are active**.

VibeCAD declares the shipped application, authenticated local daemon, task
store, and managed Worker product on macOS and Windows x86-64. Windows uses
native ACL, HANDLE/FileID, named-pipe, Job Object, and reparse-point boundaries;
it does not emulate the Darwin UID, descriptor-transfer, or process-group model.

This document records the evidence that activated the Windows support claim.
Every phase remains fail-closed: a later phase cannot compensate for a missing
or failed earlier phase.

## Fixed target

- Platform identity: `windows.x86_64`.
- Local hardware: the maintainer's native dual-boot Windows installation.
- Hosted W0 compatibility runner: `windows-2022`, never `windows-latest`.
- Native W1 runner: an x64, dedicated, disposable self-hosted Windows runner
  carrying the `vibecad-w1-standard-user-disposable` label.
- Managed runtime: Python 3.12 and the repository-pinned FreeCAD 1.1.0 runtime.
- Source identity: one exact, clean Git commit used by hosted and dual-boot runs.
- Public schema: unchanged unless a separately reviewed migration says otherwise.

W1 deliberately runs with `LongPathsEnabled=0` under
`HKLM\SYSTEM\CurrentControlSet\Control\FileSystem` and with a non-elevated
token. The interactive user may belong to the Administrators group, but the
qualification process must not be elevated. Administrator privilege, registry
changes, reboots, Developer Mode, drive mappings, and a specially short
`VIBECAD_HOME` are not product prerequisites.

The W1 bind step first proves that the real default home is absent, then uses
the product's native Windows filesystem helpers to create the home, `data`
directory, and durable-data canary with explicit protected current-user DACLs.
PowerShell must not pre-create any of them with an inherited DACL.

GitHub-hosted Windows runners run as Administrator with UAC disabled, so they
cannot supply W1's standard-user-token evidence. They supply W0 contract
evidence and the release workflow's exact-package W4 recheck only. The W1 runner
must be started interactively by the normal user, not installed as an elevated
service. Its user profile is disposable because W1 exercises the real default
`%LOCALAPPDATA%\VibeCAD`, requires that home not exist before the job, and
retains its durable-data canary after the product uninstaller removes runtime.

Windows installation uses an automatically created, ACL-protected physical
package cache whose root is at most 40 characters. `micromamba 2.5.0-2` is
restricted to `create --download-only`, which solves, downloads, and extracts
the flat cache. `micromamba 2.8.0-0` then performs the authoritative
`create --offline` link into the durable managed prefix. The exact
`viskores=1.1.1=cpu_h4b717ef_1` build is required because that conda-forge build
shortens the affected Windows header paths. The older micromamba never links or
runs the resulting environment. Its private download prefix, the physical
cache, and the recovery record are transient and must be absent when the
transaction finishes.

The cache DACL, token marker, identity checks, and helper receipt prevent
accidental reuse, stale-session deletion, and non-owner access. They are not a
security boundary against a malicious process already running under the same
Windows user SID: that process has the same user authority. W0/W1 therefore
claim deterministic cleanup and fail-closed recovery under non-malicious local
races, not isolation from a hostile same-SID process.

Windows ARM is outside this qualification. A hosted VM pass is not a substitute
for the native dual-boot product gate, and a native pass is not a substitute for
the reproducible hosted contract gate.

External baselines used by this plan:

- [GitHub-hosted runner reference](https://docs.github.com/en/actions/reference/runners/github-hosted-runners)
  documents both the supported Windows labels and the hosted Windows
  Administrator/UAC configuration that excludes those images from W1;
- [GitHub runner image catalog](https://github.com/actions/runner-images)
  documents the moving `windows-latest` alias, which is why this plan pins a
  versioned label;
- [conda-forge FreeCAD files](https://anaconda.org/conda-forge/freecad/files)
  publishes FreeCAD 1.1.0 for `win-64`, including Python 3.12 builds.
- [micromamba releases](https://github.com/mamba-org/micromamba-releases/releases)
  provide the two exact Windows executables whose checksums are verified before
  either is accepted by the installer.

## Gate sequence

### W0 — hosted contracts

Run the `Windows qualification` workflow with `phase=contracts`. It executes on
the pinned `windows-2022` image and covers the Windows platform mapping, runtime
paths, micromamba download contract, DLL search preparation, status/installer
fallbacks, private-cache ACL/path-budget/identity cleanup, and Windows file-lock
adapter contract. The job selects only tests marked `windows_contract`;
POSIX-only daemon, store, descriptor-transfer, and process-group tests remain
explicit W2 blockers and are not presented as Windows evidence.

Exit criteria:

- the frozen dependency install succeeds;
- Ruff and every selected non-slow test pass;
- the evidence artifact records the exact commit and Windows host metadata;
- no test is made green by changing the product claim to a weaker boundary.

### W1 — managed runtime

Run the workflow with `phase=managed-runtime` while a freshly provisioned native
runner with the W1 labels is online. The gate refuses an inherited
`VIBECAD_HOME`, requires the real default home not to exist, and checks the final
managed-prefix length against the reviewed 80-character budget before creating
anything there. It installs the current managed runtime, imports FreeCAD through
the managed Python, fingerprints Windows/Python/FreeCAD, and runs a real headless
create/recompute/save/export/reopen round trip. The Worker/store boundary is not
part of W1 because its descriptor transfer and revision limits are W2 work. W1
also plants durable-data and hostile-environment canaries. It removes the
authorized runtime through the product uninstaller while retaining the durable
data canary. Product Task Kernel behavior beyond safe retirement remains part of
the W2 boundary.

While the installer is running, the gate polls the home-level recovery receipt
long enough to bind the actual short physical root. It requires schema `1` in
the `active` state. Evidence retains only the receipt schema, state, and root
needed for cleanup assertions; it never copies
the ownership token or other authorization material. After the installer exits,
the gate derives the private `tmp\m25` and `tmp\d25` paths from that observed
root and proves that the root, both staging paths, and the receipt are gone.
During installation it also polls the exact two micromamba executable paths and
reduces their command lines to non-sensitive operation facts. Raw command lines,
receipt tokens, and child environments are not copied into the transaction
evidence. The final evidence binds both executable SHA-256 digests to the source
pins, derives `win-64` from installed `conda-meta` JSON, and checks that the
cache-cleanup helper module has exited. Sanitized installer stdout and stderr are
retained for failure diagnosis.

Exit criteria:

- the selected conda subdir is proven by installed metadata to be exactly
  `win-64`;
- no `VIBECAD_HOME` override is present, the default home was initially absent,
  its first creation used the product's native protected-DACL initializer, and
  its final managed prefix fits the reviewed 80-character budget;
- the process is non-elevated and records `LongPathsEnabled=0` without changing
  that registry value;
- ambient `CONDA_*`, `MAMBA_*`, `CONDARC`, `MAMBARC`, and `XDG_CACHE_HOME`
  canaries do not affect the package transaction or receive files;
- `micromamba 2.5.0-2` performs only the flat-cache `--download-only` phase,
  `micromamba 2.8.0-0` performs the `--offline` link, and the installed metadata
  contains `viskores=1.1.1=cpu_h4b717ef_1`; both binaries match their embedded
  reviewed SHA-256 digests;
- managed Python imports FreeCAD 1.1.0 without relying on a system FreeCAD;
- FreeCAD create/recompute/save/export/reopen/close succeeds headlessly;
- the short physical cache root is no longer than 40 characters, and it, the
  2.5 download prefix, and the recovery record are absent after installation;
- no executable under the managed runtime, package-cache helper, or
  `windows_job_runner.py --gate` coordinator remains after the gate;
- the authorized runtime is removed while the exact durable-data canary remains.

### W2 — Windows product security boundary

This phase is complete. Native Windows implementations and negative tests close
each corresponding Darwin-only assumption:

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

The canonical `windows.x86_64` packaged attestation resource and source pin are
selected from the trusted runtime platform identity. Native generation recorded
21 receipts, 126 formal operations, and 102 native types; the release matrix
runs the same generator in `--check` mode from the exact wheel-installed managed
Python on Windows and both supported macOS architectures.

The activation change:

- makes `release.yml` quality and reviewed-attestation matrices depend on Windows;
- verifies the exact wheel-installed Windows resource, not checkout-only bytes;
- keeps Intel macOS, ARM macOS, and Windows resources independently pinned;
- updates README/architecture/support metadata from Darwin-only;
- contains the reviewed cross-platform diff report;
- has a successful cleanup receipt for the native Windows test environment.

The publishers depend on the complete reviewed-attestation matrix, so a failed
or missing Windows member blocks both PyPI and MCPB publication.

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

Provision a dedicated disposable Windows user profile and a fresh clone at
`EXPECTED_COMMIT`. Start PowerShell normally, never with **Run as administrator**.
The default `%LOCALAPPDATA%\VibeCAD` must be unused; do not redirect it to a
timestamped `%TEMP%` path. A temporary home under `%TEMP%` commonly pushes the
final conda prefix past the reviewed 80-character budget and does not exercise
the product default.

Before registering the runner, use the checkout to perform this fail-closed
preflight:

```powershell
$ErrorActionPreference = "Stop"
git status --porcelain=v1
$expectedCommit = (git rev-parse HEAD).Trim()
$qualificationRef = (git branch --show-current).Trim()
if (-not $qualificationRef) {
  throw "check out the pushed qualification branch rather than a detached HEAD"
}
$expectedCommit
$inheritedHome = Get-Item Env:VIBECAD_HOME -ErrorAction SilentlyContinue
if ($inheritedHome) { throw "W1 forbids a VIBECAD_HOME override" }
$externalRuntime = Get-Item Env:VIBECAD_FREECAD_ENV -ErrorAction SilentlyContinue
if ($externalRuntime) { throw "W1 forbids an external FreeCAD override" }
if ([string]::IsNullOrWhiteSpace($env:LOCALAPPDATA)) {
  throw "W1 requires the standard-user LOCALAPPDATA location"
}
$longPaths = Get-ItemPropertyValue `
  -LiteralPath "HKLM:\SYSTEM\CurrentControlSet\Control\FileSystem" `
  -Name LongPathsEnabled `
  -ErrorAction SilentlyContinue
if ($null -eq $longPaths) { $longPaths = 0 }
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = [Security.Principal.WindowsPrincipal]::new($identity)
$isElevated = $principal.IsInRole(
  [Security.Principal.WindowsBuiltInRole]::Administrator
)
if ($longPaths -ne 0) { throw "W1 requires LongPathsEnabled=0" }
if ($isElevated) { throw "Start a normal, non-elevated PowerShell for W1" }
uv sync --frozen
$preflight = & ".\.venv\Scripts\python.exe" -c @'
import json, os
from vibecad.runtime import paths, spec
home = paths.vibecad_home().resolve(strict=False)
prefix = paths.env_prefix().resolve(strict=False)
print(json.dumps({
    "home": str(home),
    "home_exists": os.path.lexists(home),
    "prefix": str(prefix),
    "prefix_length": len(str(prefix)),
    "prefix_budget": spec.WINDOWS_MAX_ENV_PREFIX_LENGTH,
    "member_budget": spec.WINDOWS_REVIEWED_MAX_ENV_MEMBER,
}))
'@ | ConvertFrom-Json
$expectedHome = [IO.Path]::GetFullPath((Join-Path $env:LOCALAPPDATA "VibeCAD"))
if (-not $preflight.home.Equals($expectedHome, [StringComparison]::OrdinalIgnoreCase)) {
  throw "the checkout did not resolve the real default LOCALAPPDATA VibeCAD home"
}
if ($preflight.home_exists) {
  throw "the disposable W1 user's default VIBECAD_HOME must be absent"
}
if (
  $preflight.prefix_length -gt $preflight.prefix_budget -or
  ($preflight.prefix_length + 1 + $preflight.member_budget) -gt 259
) {
  throw "the default managed prefix exceeds the reviewed legacy-path budget"
}
uv run --frozen ruff check .
uv run --frozen pytest -q -m "windows_contract and not slow" `
  tests/test_platform.py `
  tests/test_paths.py `
  tests/test_micromamba.py `
  tests/test_freecad_env.py `
  tests/test_status.py `
  tests/test_local_daemon.py `
  tests/test_installer.py `
  tests/test_uninstall.py `
  tests/test_workflow_lease.py `
  tests/test_windows_package_cache.py `
  tests/test_windows_job_runner.py `
  tests/test_windows_qualification_plan.py
```

Register a repository-scoped self-hosted runner interactively under that same
standard user with `config.cmd --ephemeral`, the default `self-hosted`, `Windows`,
and `X64` labels, and the custom `vibecad-w1-standard-user-disposable` label. Do
not install it as an elevated service or reuse it for another job. The
version-controlled workflow is the canonical W1 procedure: it owns
receipt polling, normalized command observation, hashes, metadata evidence,
FreeCAD modeling, product uninstall, process-leak checks, and artifact upload.
Dispatch it from the Actions UI or with GitHub CLI:

```powershell
gh workflow run windows-qualification.yml `
  --ref $qualificationRef `
  -f phase=managed-runtime
```

The dispatch first runs W0 on `windows-2022`, then routes W1 only to that native
runner. Use a newly provisioned disposable runner/profile for every later
`product` or `attestation` dispatch because the preceding gate intentionally
retains its durable-data canary. A synchronous ad-hoc installer invocation is
diagnostic only: it cannot capture the transient receipt or the two live command
shapes and therefore is not release qualification evidence.

### Before returning to macOS

1. Verify the workflow's exact-runtime uninstaller removed
   `%LOCALAPPDATA%\VibeCAD\runtime` while the default home's
   `data\qualification-canary.txt` is unchanged.
2. Verify no FreeCAD, managed Python, Worker, micromamba, or pytest process remains.
3. Verify no command line contains
   `vibecad.runtime.windows_package_cache --cleanup-helper` or a
   `windows_job_runner.py` invocation with `--gate`.
4. Preserve the downloaded evidence artifact and its SHA-256 manifest on the
   shared volume, then unregister and discard the dedicated runner/profile.
5. Do not commit raw host paths, usernames, environment variables, or temporary
   logs. Only canonical reviewed attestation resources and sanitized summaries
   belong in Git.

After rebooting, compare the Windows evidence commit to the final integration
commit before accepting or generating any release resource.

## Current activation state

W0–W4 are active release evidence for macOS and Windows x86-64. The manual
workflow remains the deeper native standard-user qualification and cleanup
receipt; `release.yml` independently repeats hosted contracts, full non-slow
quality, exact wheel installation, platform identity, packaged resource decode,
and canonical `--check` before either publisher can run. Linux and Windows on
ARM remain outside the product support declaration.
