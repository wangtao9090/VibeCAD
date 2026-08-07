# Guided Photo v1

Use this host-side contract for ordinary photos of a physical mechanical part. It is a
capture and evidence workflow, not a new MCP schema. Keep CAD writes on the existing
`REQUIRE_REVIEW` Task Kernel path and use `references/parametric-design-ir-v1.md` for the
eventual model.

## Supported envelope

Admit only one single rigid mechanical part with:

- a separable background and the complete exterior in frame;
- limited glare, blur, perspective distortion, and occlusion;
- no visible deformation between views;
- an extruded 2.5D profile or a revolved profile expressible by the bounded
  Sketcher/PartDesign feature chain;
- direct evidence for every dimension and feature relationship that changes the CAD result.

Classify an assembly, soft or deformed object, freeform/sculptural surface, heavily occluded
part, or unsupported feature history as `OUT_OF_ENVELOPE`. Do not approximate it as a supported
part.

## Readiness result

Choose exactly one host-local result before creating a CAD Task:

- `PHOTO_READY`: the envelope, capture-quality, scale, and geometry-completeness gates all pass;
- `NEEDS_CAPTURE`: one additional view or measurement can close a named blocking fact;
- `OUT_OF_ENVELOPE`: the object or required result cannot fit the bounded mechanical contract.

These labels are host reasoning outcomes. Do not put them on the VibeCAD wire or invent a new
durable status.

## Capture-quality gate

Check all supplied photos before measuring geometry:

1. Confirm same object and same physical state across every source.
2. Confirm background separation around the silhouette.
3. Reject motion blur, clipped edges, glare that erases a boundary, and perspective that makes
   the intended measurement plane unusable.
4. Confirm each geometry-changing face or feature has a direct view. Treat an unseen back,
   underside, cavity, or hole termination as unknown.
5. Down-rank duplicate photos and detail crops; they do not establish a second view role.

For an extruded part, request:

- one profile-normal view for the complete 2D profile;
- one depth-normal view for extrusion depth, wall thickness, and through/blind termination;
- one oblique topology view to disambiguate which boundaries and holes belong together;
- a back or underside view only when it changes the model.

For a revolved part, request:

- one side profile with the complete axis, shoulder positions, axial lengths, and diameters;
- one axis-normal end view for concentricity, bores, and non-axisymmetric exceptions;
- one oblique topology view to expose shoulder and hole relationships.

Ask for one concrete recapture or measurement request at a time. Name the blocked fact and the
preferred camera direction, for example: “Photograph the right side nearly square-on so overall
depth and whether the hole is through can be seen.”

## Absolute-scale gate

Prefer a direct user measurement in millimetres. For an extruded part, obtain at least one
profile dimension and the extrusion depth. For a revolved part, obtain at least one axial length
and one diameter. Obtain every other independent dimension required by the final IR.

Accept a coplanar scale reference only when it lies on the same physical plane as the measured
boundary, remains fully visible, and that plane is approximately normal to the camera or has
explicit camera calibration. A ruler elsewhere in the scene, a known object at another depth,
screen size, focal-length metadata alone, or one global pixel ratio across perspective depth is
not scale evidence.

Never infer extrusion depth, hidden thickness, or diameter from perspective. If absolute scale is
missing, return `NEEDS_CAPTURE`; do not create a proportional CAD placeholder.

## Evidence and geometry-completeness gate

Build the ordinary evidence matrix before authoring CAD. For each geometry-affecting fact record:

- parameter or relationship name;
- value and unit when dimensional;
- source index and known view role;
- `confirmed`, `inferred`, or `unknown` status;
- whether it blocks the model;
- the exact user answer when a measurement or candidate branch was confirmed.

Pass the geometry-completeness gate only when:

- every independent profile, depth, diameter, location, extent, and direction has confirmed
  evidence;
- every Hole/Pocket termination and every exterior-vs-interior boundary is known;
- no supplied views conflict;
- every inferred fact that changes CAD has been explicitly confirmed by the user;
- the resulting part fits the bounded ParametricDesignIR feature chain.

Silhouette similarity, symmetry, common manufacturing practice, or a plausible wall thickness is
never sufficient by itself.

## Candidate branches and correction

When two or more hidden structures remain plausible, show a small candidate branch set before
CAD creation. Name each differing parameter or feature and what evidence would distinguish it.
Do not submit alternative candidates as confirmed geometry.

After the user selects a branch or supplies a missing measurement:

1. discard the provisional plan;
2. rebuild the evidence matrix from the original sources plus the exact new answer;
3. rerun the capture, scale, and geometry-completeness gates;
4. author one fresh bounded ParametricDesignIR only after all gates pass.

Do not silently patch an old candidate, carry an unselected branch into the IR, or reuse a stale
acceptance envelope.

## Task and review handoff

On `PHOTO_READY`, read `references/parametric-design-ir-v1.md`, call `get_capabilities`, and use
the ordinary `create_task` → `submit_model_program` workflow with `require_review`. Bind confirmed
facts to `source_refs`; keep unconfirmed inference outside the IR. Verify dimensions, DoF, BRep,
single-solid status, and an edit probe before presenting the draft.

On `NEEDS_CAPTURE`, stop before `create_task` and return the single bounded request. On
`OUT_OF_ENVELOPE`, explain the unsupported property and do not create a CAD Task.
