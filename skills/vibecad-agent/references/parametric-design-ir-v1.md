# ParametricDesignIR v1 host authoring contract

Read this reference only when `get_capabilities` reports the ModelProgram operation
`create_parametric_design` with argument value shape `parametric_design_ir`. It is a strict JSON
wire contract for one editable PartDesign Body with optional native Pattern/Mirror nodes and
Fillet/Chamfer tail. It permits no
Python, macros, FreeCAD code/strings, or arbitrary operations.

## Admission boundary

- Only facts backed by executable evidence may enter the IR. Evidence `status` is one of
  `confirmed`, `calibrated`, or `cross_view_derived`; `origin` is one of `user`, `image`,
  `multi_view`, `imported`, or `system`.
- Keep `inferred` and `unknown` facts outside the IR. Ask for confirmation when they change
  geometry. Do not submit an unscaled image as absolute millimetres.
- Every mapping is strict: include every field shown below, including required `null`, `false`,
  and empty-list values. Do not add fields.
- Every IR-local identity is `ir_<kind>_<32 lowercase hex>`. Use the full kind names in the table
  below; abbreviations such as `ir_param_`, `ir_geom_`, `ir_const_`, and `ir_feat_` are invalid.
  Identities are unique across the complete design.
- Use `schema_version: 1` on the root and every nested object. Units are exactly millimetres and
  degrees.

| object | required identity prefix |
|---|---|
| design | `ir_design_` |
| body | `ir_body_` |
| evidence | `ir_evidence_` |
| parameter | `ir_parameter_` |
| datum plane | `ir_datum_` |
| sketch | `ir_sketch_` |
| geometry | `ir_geometry_` |
| constraint | `ir_constraint_` |
| feature | `ir_feature_` |

Generate suffixes mechanically as zero-padded counters, for example
`ir_parameter_00000000000000000000000000000001`. Mnemonic/repeating hex is unsafe. Declare each ID
once, then copy that exact declared string into every reference. Before serialization, ensure every
referenced ID is byte-for-byte present in the declarations and none has 31 or 33 hex digits.

## Root and nested shapes

Without edge treatments, the root contains exactly:

```text
schema_version, id, name, units, body, evidence, parameters,
datum_planes, sketches, features
```

Add `edge_treatments` only when nonempty; never send `"edge_treatments": []`.

`units` is `{schema_version, length: "mm", angle: "deg"}`. `body` is
`{schema_version, id, name}`.

Each evidence record contains:

```text
schema_version, id, status, origin, source_refs[1+], description|null
```

`source_refs` must contain at least one nonempty, unique reference. A current user instruction may
use a stable host-local label such as `user:current_request`; an image may use the host-visible
attachment identifier. Prefer one evidence record with multiple real source refs over redundant
records, and never create an evidence record with an empty `source_refs` array.

Each independent parameter contains:

```text
schema_version, id, name, kind, value, unit, evidence_ids[],
minimum|null, maximum|null, public
```

`kind` is `length` with unit `mm` or `angle` with unit `deg`. A dimensional constraint and every
feature parameter reference a parameter identity. Prefer user-meaningful confirmed dimensions as
`public: true`; private independent construction origins use `public: false`. Do not include an
`expression` key on an independent parameter.

A derived parameter adds exactly this optional field to the independent shape:

```json
{
  "expression": {
    "schema_version": 1,
    "constant": 0,
    "terms": {
      "ir_parameter_00000000000000000000000000000001": 1,
      "ir_parameter_00000000000000000000000000000002": -2
    }
  }
}
```

This means `constant + sum(coefficient * source_parameter)`. It is not a string and cannot contain
functions or arbitrary FreeCAD syntax. A derived parameter must use `public: false`, contain one to
eight unique same-unit source parameters with finite nonzero coefficients, be acyclic, and declare
an initial `value` exactly matching the evaluated expression. Omit `expression` for an independent
parameter; do not send `expression: null`. The complete design may contain at most 128 parameters.
Count every public dimension and every private independent or derived coordinate parameter before
submission; do not retry a design that exceeds this limit without reducing redundant dimensions.

An origin sketch plane is:

```json
{"schema_version":1,"kind":"origin","origin":"xy","datum_id":null}
```

`origin` may also be `xz` or `yz`. Use it directly when it is sufficient and keep `datum_planes`
empty. Create an explicit datum only when a sketch actually references it. A datum contains
`schema_version`, `id`, `name`, `origin_mm[3]`, unit-length orthogonal `normal[3]` and `x_axis[3]`,
and at least one `evidence_ids` entry; its sketch plane uses `kind: "datum"`, `origin: null`, and the
exact `datum_id`. Never add an unused convenience datum.

Each sketch contains:

```text
schema_version, id, name, role, plane, geometries[], constraints[], evidence_ids[]
```

`role` is `profile`, `hole_locations`, or `reference`. Each geometry contains
`schema_version`, `id`, `kind`, `dimensions`, `construction`, and `evidence_ids`:

| kind | exact dimensions |
|---|---|
| `point` | `x_mm`, `y_mm` |
| `line` | `x1_mm`, `y1_mm`, `x2_mm`, `y2_mm` |
| `circle` | `cx_mm`, `cy_mm`, `radius_mm` |
| `arc` | `cx_mm`, `cy_mm`, `radius_mm`, `start_angle_deg`, `sweep_angle_deg` |
| `slot` | `x1_mm`, `y1_mm`, `x2_mm`, `y2_mm`, `width_mm` |

For `slot`, the two pairs are end-cap centers and `width_mm` is full width. Only horizontal or
vertical centerlines compile: one slot becomes two native lines, two native semicircular arcs, and
a closed editable wire. Do not attach IR constraints to a slot; its five dimensions are
authoritative. Oblique slots fail closed. Use one profile sketch and Pocket per through-slot.

Each reference is `{schema_version, target, point}`. `target` is a geometry identity or `@origin`,
`@x_axis`, `@y_axis`; `point` is `whole`, `start`, `end`, or `center` as appropriate. Each
constraint contains:

```text
schema_version, id, kind, references[], parameter_id|null, evidence_ids[]
```

Constraint reference counts are: two for `coincident`, `parallel`, `perpendicular`, `tangent`,
`equal`, `distance`, `distance_x`, `distance_y`, and `angle`; one for `horizontal`, `vertical`,
`length`, `radius`, and `diameter`; three for `symmetric`. Dimensional kinds require a matching
evidence-backed parameter; nondimensional kinds require `parameter_id: null`. Aim for solver
`DoF=0` without redundant, conflicting, or malformed constraints.

Initial coordinates are not constraints. A rectangle needs endpoint coincidences, orientation,
width/height, and an origin anchor such as line `start` coincident with
`{schema_version: 1, target: "@origin", point: "center"}`. A circle needs radius/diameter and, when
off-origin, center X/Y. For `distance_x|distance_y`, reference order is semantic: the first
reference is `@origin` center and the second the circle center. Do not reverse them. Require
`DoF=0` before submission.

Use the verified independent-coordinate recipe for an eight-edge rounded rectangle; relationship-
only solving is unreliable. Each line gets orientation, length, and positive X/Y distances from
`@origin` to its start. Each quarter arc gets shared public radius, private center X/Y, and two
endpoint coordinates. For CCW arcs starting at 270/0/90/180 degrees use, respectively:
bottom-right `start_x = center_x`, `end_y = center_y`; top-right `start_y = center_y`,
`end_x = center_x`; top-left the bottom-right pair; bottom-left the top-right pair. Never constrain
the radial extreme coordinates. This yields 16 independent line constraints and 20 independent arc
constraints. For width `W`, height `H`, radius `R`, origins `OX/OY`, derive
`straight_width=W-2R`, `straight_height=H-2R`, `left/right_arc_x=OX+R/OX+W-R`,
`bottom/top_arc_y=OY+R/OY+H-R`, `right_x=OX+W`, and `top_y=OY+H`. Use private
evidence-backed expression parameters, reuse equal coordinates, add no `coincident`, `tangent`, or
`equal` constraints, and require `DoF=0`.

Each feature contains every field below:

```text
schema_version, id, name, kind, sketch_id, base_feature_id|null,
parameters{}, evidence_ids[], extent|null, axis|null,
location_geometry_ids[], reversed, symmetric
```

Supported profiles:

| kind | parameters | extent | other rules |
|---|---|---|---|
| `pad` | `{"length": parameter_id}` | `length` | first feature allowed |
| `pocket` | `{}` or `{"length": parameter_id}` | `through_all` or `length` | profile sketch; exactly one closed live wire per Pocket feature |
| `hole` | `{"diameter": parameter_id}` plus `depth` only for length | `through_all` or `length` | `hole_locations` sketch; 1–16 nonconstruction circles sharing one plane, diameter, extent/depth, and direction; list every circle in `location_geometry_ids` |
| `revolve` | `{"angle": parameter_id}` | `null` | first feature allowed; axis is `@sketch_x`, `@sketch_y`, or a construction line |
| `linear_pattern` | `{"length": parameter_id}` | `null` | no sketch; `direction` is `x_axis|y_axis|z_axis`; 2–16 occurrences |
| `circular_pattern` | `{"angle": parameter_id}` | `null` | no sketch; `axis` is `@body_x|@body_y|@body_z`; 2–16 occurrences |
| `mirror` | `{}` | `null` | no sketch; `mirror_plane` is `xy_plane|xz_plane|yz_plane` |

Features form one linear chain: the first base is null, and every later `base_feature_id` is the
immediately previous feature. Each sketch is consumed once. A profile must close safely and each
feature must produce one valid solid. Among primitive features, `axis` is required only for Revolve
and must be `null` for Pad, Pocket, and Hole. `location_geometry_ids` is populated only for Hole.
For a positive-Z pad on
the origin XY plane, the verified same-plane through-hole profile uses `reversed: true`; always
verify subtractive direction through the deterministic shape checks.

### Native Pattern/Mirror nodes

They remain `ir_feature_` records, set `sketch_id: null`, and add exactly:

```text
source_feature_id, direction|null, mirror_plane|null, occurrences|null
```

`source_feature_id` names one earlier Pad/Pocket/Hole/Revolve, never a pattern, future/treatment
node, FreeCAD name, or index. `base_feature_id` still names the immediately previous chain node.

- Linear: positive mm `length`, body-axis `direction`, `axis/mirror_plane: null`, 2–16
  `occurrences`; `reversed` allowed.
- Circular: positive `angle` <=360 deg, body-origin `axis`, `direction/mirror_plane: null`, 2–16
  `occurrences`; `reversed` allowed.
- Mirror: empty parameters, one body-origin `mirror_plane`, other selectors and `occurrences: null`,
  `reversed: false`.

All require `extent: null`, empty locations, and `symmetric: false`. Only the table's Body-origin
tokens are allowed; they compile to native `PartDesign::LinearPattern`, `PartDesign::PolarPattern`,
or `PartDesign::Mirrored`.

Budget: four pattern/mirror nodes, 16 occurrences each, 32 total declared instances (Mirror counts
two), and eight combined feature/treatment nodes. Every node must yield one valid changed solid;
disconnected, missing-material, no-op, ambiguous-reference, or kernel failures reject atomically.

### Semantic Fillet/Chamfer tail

Treatment shape: `{schema_version, id, name, kind, base_feature_id, targets, evidence_ids}`.
`kind` is `fillet|chamfer`; IDs remain `ir_feature_`. Treatments follow PartDesign features;
the combined total is at most eight. The final treatment is the result root.

Each has 1–16 unique `{schema_version, edge, start_parameter_id, end_parameter_id}` targets.
`edge` is `{schema_version, source_feature_id, geometry_id, role, point}` naming a
Pad/Pocket/Hole/Revolve and nonconstruction geometry. Never use `EdgeN`, `FaceN`, labels, or indexes.

Use `sweep` + `start|end` for a line/arc endpoint's swept edge. Use
`section_start|section_end` + `whole` for a profile edge at either feature-direction end.

Parameters are positive mm lengths. Equal IDs mean constant Fillet; different IDs mean a linear
radius law on an oriented nonclosed edge. Targets may differ. Chamfer requires equal IDs. Variable
Chamfer, multi-point laws, tangent chains, and STEP edges are unsupported.

Edit/reload re-resolves targets; missing, ambiguous, duplicate, direction-flipped, invalid, or
kernel-failed results roll back atomically.

Origin planes use these local axes and normals:

| origin plane | sketch local X | sketch local Y | positive normal |
|---|---|---|---|
| `xy` | world X | world Y | world +Z |
| `xz` | world X | world Z | world -Y |
| `yz` | world Y | world Z | world +X |

For the current Hole backend, `reversed: true` cuts along the positive sketch normal and
`reversed: false` cuts opposite it. Thus a solid extending from the origin toward world +Y uses
`reversed: false` for an origin-XZ Hole, while a solid extending toward world +X uses
`reversed: true` for an origin-YZ Hole. This is a direction rule, not permission to guess where
material lies; required shape verification remains authoritative.

Sketch geometry coordinates are only deterministic starting geometry. Constraints must form one
independent system: dimension the minimum independent outer/thickness/position facts and let the
solver derive inner lengths. Do not constrain an outer length and its already-derived inner length
as if both were independent. For Hole circles, use `diameter` constraints backed by the exact same
parameter ID as the consuming Hole feature's `parameters.diameter`; do not add a separate radius
parameter for the same observed diameter.

For multi-view work, one image count does not equal one evidence count. A `cross_view_derived`
evidence record must refer to facts produced from at least two distinct known view roles belonging
to the same object, state, and scale. Keep a dimension or relationship out of the IR when the views
conflict or leave it unresolved. Multiple circles may share one Hole only under the common-plane,
common-diameter, common-extent/depth, and common-direction rule above; the compiler verifies a real
material-removal point on every declared axis. Use separate Hole features for different planes or
hole specifications. Use sequential one-wire Pocket sketches for multiple cutouts; do not submit a
single multi-loop Pocket.

## ModelProgram and acceptance

The object below is the complete ModelProgram root. Submit this entire object as `program_json`;
do not submit only the operation inside `operations`. Replace `TASK_ID`, `BASE_REVISION`, the empty
`design`, the expected values, and tolerances with values justified by the current task and evidence.
Keep every other field, including all explicit `null`, `true`, and empty arrays/objects.
Serialize the final `program_json` compactly, without indentation or insignificant whitespace; this
preserves the exact contract while avoiding unnecessary host/MCP argument growth.

```json
{
  "schema_version": 1,
  "task_id": "TASK_ID",
  "base_revision": "BASE_REVISION",
  "operations": [
    {
      "schema_version": 1,
      "id": "create-editable-design",
      "op": "create_parametric_design",
      "target": {},
      "args": {"design": {}},
      "preserve": [],
      "source": "model",
      "depends_on": []
    }
  ],
  "acceptance": {
    "schema_version": 1,
    "id": "accept-image-derived-design",
    "criteria": [
      {
        "schema_version": 1,
        "id": "expected-bounding-box",
        "kind": "geometry",
        "check": "bbox",
        "target": "body",
        "expected": [80, 50, 8],
        "tolerance": 0.05,
        "parameters": {"unit": "mm"},
        "required": true
      },
      {
        "schema_version": 1,
        "id": "expected-volume",
        "kind": "geometry",
        "check": "volume",
        "target": "body",
        "expected": 31371.6814693,
        "tolerance": 0.1,
        "parameters": {"unit": "mm^3"},
        "required": true
      },
      {
        "schema_version": 1,
        "id": "valid-shape",
        "kind": "topology",
        "check": "valid_shape",
        "target": "body",
        "expected": true,
        "tolerance": null,
        "parameters": {},
        "required": true
      },
      {
        "schema_version": 1,
        "id": "one-solid",
        "kind": "topology",
        "check": "solid_count",
        "target": "body",
        "expected": 1,
        "tolerance": null,
        "parameters": {},
        "required": true
      }
    ]
  }
}
```

The operation object shown in `operations` is one ModelCommand, not a complete ModelProgram. The
strict ModelProgram root has exactly `schema_version`, `task_id`, `base_revision`, `operations`, and
`acceptance`. Its `task_id` and `base_revision` must exactly match the current persisted task. The
strict acceptance root has exactly `schema_version`, `id`, and `criteria`; every criterion has
exactly `schema_version`, `id`, `kind`, `check`, `target`, `expected`, `tolerance`, `parameters`, and
`required`.

For image-derived mechanical parts, require at least geometry `bbox` and `volume` with justified
finite tolerances plus topology `valid_shape: true` and `solid_count: 1`. Compute volume from the
confirmed design intent, including every subtractive feature; do not copy the sample numbers unless
they are correct for the current part. Do not accept a draft merely because the operation returned
`ok`; inspect all verifier verdicts and keep the project HEAD unchanged until explicit review
acceptance.

Before submission, check all of the following:

1. The submitted object is the complete ModelProgram root, not a ModelCommand or bare IR.
2. Every IR identity uses a full allowed prefix and 32 lowercase hexadecimal characters.
3. Every evidence record has at least one source ref; every parameter, datum, dimensional constraint,
   and feature has the required evidence binding.
4. Every nested strict object includes its required fields, including explicit nulls and empties.
5. Every sketch consumed by a feature is closed where required, fully constrained, and anchored.
6. Every feature forms one linear single-solid chain and every subtractive direction is verified.
7. All four required acceptance criteria are present with evidence-derived expected values.
8. The final `program_json` string is compact JSON rather than pretty-printed JSON.
9. Every cross-view fact cites distinct known view roles from one object/state/scale, with no
   unresolved depth, hidden geometry, or dimensional conflict promoted into the IR.
10. Every grouped Hole contains at most 16 locations with one shared plane, diameter,
    extent/depth, and direction; every Pocket sketch has exactly one live closed wire.
11. Every sketch dimension set is independent, every non-Revolve feature has `axis: null`, and
    every Hole circle reuses the Hole feature's diameter parameter through a `diameter` constraint.
12. The complete design contains no more than 128 parameters, including private derived-coordinate
    parameters used to fully constrain rounded rectangles; each derived parameter uses the bounded
    affine object, is private, same-unit, acyclic, and has a matching initial value.
13. For WorkBuddy 5.3.5, the adapter request root has exactly four fields—`schema_version`,
    `task_id`, `expected_generation`, and the complete ModelProgram under `program`—and the same
    program is not first sent through MCP.
