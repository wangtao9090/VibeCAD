# CAD files in Git — product decision for VibeCAD

> Decision date: 2026-08-01
>
> Scope: native FreeCAD files, neutral CAD exports, VibeCAD design intent, and
> future Agent Teams. This document is a product/storage decision, not a claim
> that Git integration is implemented.

## 1. Decision

Git is useful for CAD projects, but it must not become VibeCAD's live CAD
database or its semantic merge engine.

VibeCAD keeps its existing Task Kernel, immutable Revision graph, HEAD CAS,
candidate isolation, verification, and review as the authoritative design
history. A future Git integration may export or mirror accepted revisions for
backup, review, release, and reproducibility. It must not watch a working
`FCStd` file and silently turn every save into a VibeCAD commit.

Recommended storage split:

| Content | Git suitability | VibeCAD policy |
|---|---|---|
| ModelProgram / design intent / parameter source | strong | normal Git text |
| AcceptanceSpec, observation summary, manifest, provenance | strong | normal Git text |
| scripts such as CadQuery, OpenSCAD, or controlled Python | strong when they are the design source | normal Git text, subject to the project's execution policy |
| accepted native `FCStd` | snapshot only | optional Git LFS artifact |
| accepted STEP export | strong interchange/release snapshot | normal Git when small and deterministic; Git LFS when large; never line-merge |
| accepted STL export | manufacturing/delivery snapshot only | usually Git LFS; ASCII STL is still generated mesh data, not source |
| accepted DXF export | release/interchange snapshot | normal Git or LFS by size and determinism; never assume semantic merge |
| managed checkout, draft working copy, temp export | none | ignore; never commit |

## 2. Why native CAD files do not behave like source code

FreeCAD documents use the `.FCStd` zip-based compound format. The archive can
contain XML plus BRep and other payloads; the contained files are interlinked.
This makes an accepted file a useful recoverable snapshot, but ordinary Git
line diff and three-way merge do not understand feature-tree dependencies,
topological references, recomputation, or whether the merged model opens and
remains geometrically valid.

Git's own documentation treats binary paths as having no generally defined
merge semantics: the built-in binary merge keeps one side and leaves a
conflict for a person to resolve. A text conversion driver can make a binary
file easier to inspect, but it does not make the converted text a safe CAD
merge representation.

Sources:

- [FreeCAD manual — `.FcStd` is a zip-based compound format](https://www.freecad.org/manual/a-freecad-manual.pdf)
- [Git attributes — binary diff and merge behavior](https://git-scm.com/docs/gitattributes)
- [Pro Git — text conversion can display binary-file differences](https://git-scm.com/book/en/v2/Customizing-Git-Git-Attributes)

## 3. What Git LFS solves

Git LFS replaces a large file in Git history with a small pointer and stores
the file bytes in an LFS object service. This improves clone behavior and keeps
large binary blobs out of the ordinary Git object database.

It does not provide CAD semantic diff or merge. On GitHub, each changed LFS
file version is stored and billed as a new complete object; a one-byte edit to
a 500 MB file adds another 500 MB of LFS storage. Pull request views may show
only the pointer, so CAD review still needs a checked-out model, rendered
evidence, or VibeCAD's structured comparison.

Git LFS also has an exclusive file-locking API. That is useful if a team elects
to keep native CAD snapshots in Git, but it reinforces a serial editing model;
the current API documents the simplest case as single-branch locking. It is
not a branch merge solution.

Sources:

- [GitHub — About Git Large File Storage](https://docs.github.com/en/repositories/working-with-files/managing-large-files/about-git-large-file-storage)
- [GitHub — Git LFS storage and bandwidth accounting](https://docs.github.com/en/billing/concepts/product-billing/git-lfs)
- [GitHub — Collaboration with Git LFS](https://docs.github.com/en/repositories/working-with-files/managing-large-files/collaboration-with-git-large-file-storage)
- [Git LFS — File Locking API](https://github.com/git-lfs/git-lfs/blob/main/docs/api/locking.md)

## 4. STEP and STL are useful artifacts, not mergeable design source

STEP Part 21 is a clear-text exchange format, so Git can store it directly and
produce a textual diff. That makes STEP a much better neutral release snapshot
than FCStd for long-lived interchange. It still represents an entity/reference
graph emitted by a CAD exporter: entity numbers, ordering, numeric formatting,
headers, and exporter versions may change without a corresponding design
change. A line-level merge can therefore create a syntactically plausible but
geometrically invalid or semantically different model. VibeCAD should regenerate
and verify STEP from an accepted Revision, not accept a Git merge as authority.

STL stores a tessellated triangular surface rather than a parametric solid.
The format has ASCII and binary variants; binary is more common because it is
more compact. ASCII STL is technically diffable, but facet ordering and many
floating-point vertex lines make its diffs large and low-value. Both variants
discard the editable feature tree and exact analytic geometry, so STL is best
treated as a final 3D-printing/manufacturing deliverable. Use LFS for ordinary
binary or large STL files; use normal Git for a small deterministic ASCII STL
only when its human-readable history is genuinely useful.

Recommended priority for an accepted VibeCAD revision:

1. canonical intent, acceptance, manifest, and provenance in normal Git;
2. STEP as the preferred neutral interchange/release artifact;
3. FCStd as an optional exact native recovery snapshot;
4. STL only when it is a required downstream manufacturing deliverable.

Sources:

- [Library of Congress — STEP Part 21 clear-text exchange format](https://www.loc.gov/preservation/digital/formats/fdd/fdd000448.shtml)
- [FreeCAD documentation — supported import/export formats](https://github.com/FreeCAD/FreeCAD-documentation/blob/main/wiki/Import_Export.md)
- [Library of Congress — STL ASCII/binary triangular-mesh family](https://www.loc.gov/preservation/digital/formats/fdd/fdd000504.shtml)
- [Git attributes — binary and converted diff behavior](https://git-scm.com/book/en/v2/Customizing-Git-Git-Attributes)

## 5. VibeCAD integration boundary

A future Git export should be explicit and one-way from an accepted VibeCAD
revision:

```text
VibeCAD accepted Revision / HEAD
  -> canonical text manifest and intent
  -> optional FCStd + STEP release artifacts through Git LFS
  -> one Git commit or tag carrying the VibeCAD revision id
```

The reverse direction is an explicit import, never an automatic merge:

```text
Git checkout / external FCStd
  -> private VibeCAD candidate
  -> reopen, observe, verify
  -> explicit publish
  -> new VibeCAD Revision
```

The following are deliberately excluded from P1:

- using a Git branch as the authoritative project HEAD;
- auto-committing every FreeCAD save;
- unpacking FCStd and line-merging its XML/BRep members;
- promising semantic merges for STEP, STL, DXF, or native CAD files;
- requiring Git, GitHub, or Git LFS for local VibeCAD operation.

## 6. Agent Teams

Git worktrees are useful for isolating code and text assets. For CAD Agent
Teams, the closer product abstraction is one immutable VibeCAD base revision
with one candidate/draft per Agent. Teams may compare proposals, select one,
or later apply explicit feature-level operations. Raw Git merge of the native
CAD files is not the combining mechanism.

If native artifacts are mirrored to Git, branch-per-Agent is an audit and
distribution view of those VibeCAD candidates. It does not grant Git commit or
merge results CAD authority.
