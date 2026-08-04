# ParametricDesignIR v1 host authoring contract

Read this reference only when `get_capabilities` reports the ModelProgram operation
`create_parametric_design` with argument value shape `parametric_design_ir`. It is a strict JSON
wire contract for one editable PartDesign Body; it is not permission to run Python, FreeCAD code,
macros, expressions, or arbitrary operations.

## Admission boundary

- Only facts backed by executable evidence may enter the IR. Evidence `status` is one of
  `confirmed`, `calibrated`, or `cross_view_derived`; `origin` is one of `user`, `image`,
  `multi_view`, `imported`, or `system`.
- Keep `inferred` and `unknown` facts outside the IR. Ask for confirmation when they change
  geometry. Do not submit an unscaled image as absolute millimetres.
- Every mapping is strict: include every field shown below, including required `null`, `false`,
  and empty-list values. Do not add fields.
- Every IR-local identity is `ir_<kind>_<32 lowercase hex>`, where kind is `design`, `body`,
  `evidence`, `parameter`, `datum`, `sketch`, `geometry`, `constraint`, or `feature`. Identities are
  unique across the complete design.
- Use `schema_version: 1` on the root and every nested object. Units are exactly millimetres and
  degrees.

## Root and nested shapes

The root contains exactly:

```text
schema_version, id, name, units, body, evidence, parameters,
datum_planes, sketches, features
```

`units` is `{schema_version, length: "mm", angle: "deg"}`. `body` is
`{schema_version, id, name}`.

Each evidence record contains:

```text
schema_version, id, status, origin, source_refs[], description|null
```

Each parameter contains:

```text
schema_version, id, name, kind, value, unit, evidence_ids[],
minimum|null, maximum|null, public
```

`kind` is `length` with unit `mm` or `angle` with unit `deg`. A dimensional constraint and every
feature parameter reference a parameter identity. Prefer user-meaningful confirmed dimensions as
`public: true`; derived construction offsets may be false.

An origin sketch plane is:

```json
{"schema_version":1,"kind":"origin","origin":"xy","datum_id":null}
```

`origin` may also be `xz` or `yz`. An explicit datum plane contains `schema_version`, `id`, `name`,
`origin_mm[3]`, unit-length orthogonal `normal[3]` and `x_axis[3]`, and `evidence_ids[]`; its sketch
plane uses `kind: "datum"`, `origin: null`, and the exact `datum_id`.

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

Do not use `slot` in the current compiled envelope even though the value contract reserves it.

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
| `pocket` | `{}` or `{"length": parameter_id}` | `through_all` or `length` | profile sketch |
| `hole` | `{"diameter": parameter_id}` plus `depth` only for length | `through_all` or `length` | `hole_locations` sketch; list every nonconstruction circle in `location_geometry_ids` |
| `revolve` | `{"angle": parameter_id}` | `null` | first feature allowed; axis is `@sketch_x`, `@sketch_y`, or a construction line |

Features form one linear chain: the first base is null, and every later `base_feature_id` is the
immediately previous feature. Each sketch is consumed once. A profile must close safely and each
feature must produce one valid solid. For a positive-Z pad on the origin XY plane, the verified
same-plane through-hole profile uses `reversed: true`; always verify subtractive direction through
the deterministic shape checks.

## ModelProgram and acceptance

Submit the design only inside one ModelProgram command:

```json
{
  "schema_version": 1,
  "id": "create-editable-design",
  "op": "create_parametric_design",
  "target": {},
  "args": {"design": {}},
  "depends_on": [],
  "preserve": [],
  "source": "model"
}
```

Replace the empty design with the complete strict IR. Bind the enclosing ModelProgram to the exact
task id and current base revision. For image-derived mechanical parts, require at least geometry
`bbox` and `volume` (with finite tolerances) plus topology `valid_shape: true` and `solid_count: 1`.
Do not accept a draft merely because the operation returned `ok`; inspect all verifier verdicts and
keep the project HEAD unchanged until explicit review acceptance.
