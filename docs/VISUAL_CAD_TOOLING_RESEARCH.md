# Visual CAD mesh, sculpting, and DCC tooling research

> Research date: 2026-08-03
>
> Scope: tools and libraries for image/scan-derived Mesh, SubD, sculpting, and
> industrial freeform geometry around VibeCAD.
>
> Decision status: product-direction research only. It does not activate the
> Freeform track, admit Mesh/SubD into authoritative Revision v1, or authorize
> a new provider, external image transfer, dependency, purchase, or release.

## 1. Decision summary

VibeCAD should not ask FreeCAD to become a digital-sculpting application.
FreeCAD can import, inspect, repair, segment, cut, smooth, decimate, and convert
triangle meshes, but its product center is parametric BRep modeling rather than
brush-based sculpting or SubD control-cage authoring.

The recommended future split is:

| Outcome | Preferred engine | Role in VibeCAD |
|---|---|---|
| mechanical parametric part | FreeCAD | authoritative Sketcher/PartDesign Task path |
| point cloud, RGB-D, or mesh algorithms | Open3D | in-process or isolated geometry library |
| organic Mesh/SubD creation and user sculpting | Blender | first external DCC adapter candidate |
| industrial SubD/NURBS/BRep | Rhino or existing FreeCAD surface capabilities | later, separately approved backend |
| procedural volume/geometry networks | Houdini | optional commercial provider/backend |
| high-detail specialist sculpting | ZBrush or 3DCoat | optional user-owned round trip |
| mesh filter catalogue | PyMeshLab | optional GPL-isolated tool after license review |

For the currently approved `VCAD-A02`, none of these engines is activated.
The active provider remains deterministic and fake; real image understanding
and the Sculpture/Freeform tracks retain their later approval gates.

## 2. Mesh processing is not sculpting

Mesh processing applies explicit operations to an existing topology: repair,
hole filling, normal correction, smoothing, decimation, remeshing, boolean,
cutting, segmentation, measurement, and format conversion. These operations
are suitable for deterministic automation and quality gates.

Sculpting is interactive form authoring. A brush pushes, inflates, pinches,
creases, or smooths a local region with falloff; mature sculptors add symmetry,
dynamic topology, multi-resolution detail, and SubD control-cage workflows.
This is why a tool may be able to edit a `Mesh` without being a practical
sculpting host.

FreeCAD's official Mesh Workbench exposes a useful processing set, but the
documentation also explains that FreeCAD normally prefers higher-level BRep
solids for engineering. Directly converting a dense scan to Part faces retains
thousands of triangles and produces a heavy object with no recovered feature
history. VibeCAD should therefore use FreeCAD for inspection, section extraction,
and accepted engineering reconstruction, not for primary sculpture UX.

Sources:

- [FreeCAD Mesh Workbench](https://github.com/FreeCAD/FreeCAD-documentation/blob/main/wiki/Mesh_Workbench.md)
- [FreeCAD Mesh to Part](https://github.com/FreeCAD/FreeCAD-documentation/blob/main/wiki/Mesh_to_Part.md)

## 3. Candidate application backends

| Tool | Primary strength | Automation surface | Assessment |
|---|---|---|---|
| Blender | Mesh, SubD, sculpt, modifiers, rendering | `bpy`, `bmesh`, add-ons, background CLI | best first DCC adapter |
| Houdini | procedural geometry, volumes, node graphs, batch pipelines | HOM `hou`, `hython`, HAPI, RPC | strongest procedural alternative; commercial licensing |
| Rhino/Grasshopper | SubD, NURBS, BRep, manufacturing surfaces | RhinoCommon, Python, Rhino.Compute REST | best organic-to-engineering bridge |
| ZBrush | high-detail interactive sculpting | ZBrush 2026 Python SDK and command-line script launch | specialist user host; API is a shallow ZScript mapping |
| 3DCoat | voxel sculpt, retopology, UV, texture | embedded Python API and AppLink | useful specialist round trip |
| Maya | polygon/SubD and established studio pipelines | Python API, `maya.cmds`, `mayapy` batch | capable but heavy commercial dependency |
| Cinema 4D | modeling, procedural effects, motion graphics | Python SDK and headless `c4dpy` | possible commercial adapter, not an initial priority |

Blender is unusual because the same zero-cost, cross-platform application
offers mature manual editing, broad import/export, a deep Python API, and a
background process surface. Houdini is stronger for node-based reproducible
procedural work. Rhino is stronger when the intended result must become a
manufacturable NURBS/BRep instead of remaining an artistic mesh.

Sources:

- [Blender Python API](https://docs.blender.org/api/current/)
- [Blender command-line arguments](https://docs.blender.org/manual/en/latest/advanced/command_line/arguments.html)
- [Houdini Python scripting](https://www.sidefx.com/docs/houdini/hom/index.html)
- [Houdini command-line scripting](https://www.sidefx.com/docs/houdini/hom/commandline)
- [RhinoCommon guides](https://developer.rhino3d.com/guides/rhinocommon/)
- [Rhino.Compute guides](https://developer.rhino3d.com/en/guides/compute/)
- [ZBrush Python SDK](https://developers.maxon.net/docs/zbrush/py/)
- [3DCoat Python API](https://3dcoat.com/documentation/manual/scripting-and-core-api/python-api/)
- [Maya `mayapy`](https://help.autodesk.com/cloudhelp/2026/ENU/Maya-Scripting/files/GUID-D64ACA64-2566-42B3-BE0F-BCE843A1702F.htm)
- [Cinema 4D Python SDK](https://developers.maxon.net/docs/py/)

## 4. Algorithm libraries

Open3D and PyMeshLab are processing libraries, not substitutes for Blender's
sculpting UI.

- Open3D provides Python/C++ geometry, point-cloud registration, RGB-D/TSDF
  integration, surface reconstruction, mesh deformation, ray queries, and
  direct array access. It is a strong candidate for bounded visual-geometry
  preprocessing and verification.
- PyMeshLab exposes MeshLab's mesh filters through Python. It is useful for
  repair, reconstruction, filtering, simplification, and measurements, but its
  GPL license changes the distribution decision.

Sources:

- [Open3D documentation](https://www.open3d.org/docs/latest/)
- [PyMeshLab documentation](https://pymeshlab.readthedocs.io/en/latest/intro.html)

## 5. Cost and license boundary

The following was checked on 2026-08-03; optional service prices can change.

| Component | Cost | License/product boundary |
|---|---:|---|
| Open3D | free | MIT; suitable for the MIT VibeCAD core with notices |
| PyMeshLab | free | GPL; do not add as an ordinary VibeCAD core dependency without legal review |
| Blender application/API | free | GPL; commercial use and user-created output are allowed |
| Blender Studio | optional | `$17/month`, or `$34.50/quarter`; not required for Blender or its API |
| Blender Studio Teams | optional | starts at `$540/month`; training/assets/support service only |

Blender states that its generated artwork and `.blend`/data outputs belong to
the user. It also states that published scripts using the integral `bpy` API
must use a GPL-compatible license. Therefore the safe initial integration
shape is an explicitly installed Blender application launched as a separate,
version-pinned process with a narrow file/JSON contract. Any distributed
Blender add-on must have its own reviewed GPL-compatible licensing boundary.

PyMeshLab is free of charge but not permissively licensed. Since VibeCAD is
MIT, direct import and redistribution in the normal package is not approved by
this research. A separately installed external worker may reduce coupling, but
that boundary still needs legal review before product distribution. Open3D is
the preferred first library when its algorithms cover the requirement.

Sources:

- [Open3D MIT license](https://github.com/isl-org/Open3D/blob/main/LICENSE)
- [PyMeshLab GPL license](https://github.com/cnr-isti-vclab/PyMeshLab#license)
- [Blender license and artwork policy](https://www.blender.org/about/license/)
- [Blender Studio pricing](https://studio.blender.org/join/)

## 6. Proposed future VibeCAD seam

A future implementation should reuse the existing provider-neutral runtime
envelope but introduce a separately approved organic-geometry domain contract:

```text
ImageSet / scan / point cloud
  -> bounded observation and provenance
  -> OrganicModelIR or MeshOperationPlan
  -> Open3D preprocessing (when required)
  -> BlenderAdapter as a separate managed process
  -> .blend + mesh export + preview + validation summary
  -> explicit user review / optional manual Blender edit
  -> immutable derived artifact
```

The Agent must not emit arbitrary `bpy` Python. The adapter should translate a
strict allowlist such as import, voxel remesh, smooth, decimate, mirror,
subdivision, bounded control-point movement, preview rendering, and export.
Brush trajectories and UI-context-dependent operators should not be the first
automation contract because they are harder to replay and verify.

Mesh/SubD remains a derived artifact under durable v1. Promoting `.blend`,
Mesh, or SubD to an authoritative Revision requires a separate artifact
profile, version/identity rules, validation contract, recovery boundary, and
approval. This research intentionally makes no such scope change.
