# Guided Photo v1 public fixture set

This directory contains the fixed S40 ordinary-photo decision and CAD-outcome
fixtures. Every committed image is a metadata-free RGB PNG with a longest edge
of at most 1,600 pixels. The original public downloads, private pilot files, and
reference meshes are not committed.

The fixture data is deliberately split by authority:

- `host_inputs.json` is the only JSON payload that a host run may receive. It
  contains normalized image names, the user's request, and confirmed facts.
- `expected_outcomes.json` belongs to the routing harness. It is never placed in
  a host prompt and records the expected readiness result and whether a Task may
  be created.
- `evaluator_truth.json` belongs to the post-candidate evaluator. It is never
  provided before a positive candidate finishes.
- `source_manifest.json` records source, license, original/normalized hashes,
  and normalization provenance. It is not geometry evidence for a host.

The three positive cases cover an annular washer, a rounded-square fan spacer,
and a rectangular block with a blind pocket. The washer is reused as a
metamorphic negative with its thickness intentionally omitted. The HB frame is
a separate multiple-object negative. Negative cases must stop before
`create_task`.

The fixture proves only the bounded Guided Photo v1 envelope. It does not claim
photo-only metrology, arbitrary reverse engineering, or semantic merge with a
reference CAD file.
