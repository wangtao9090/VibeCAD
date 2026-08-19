"""Private backend-neutral intent compiler core.

Concrete source schemas and intent rule sets live outside this package.  The
core only selects exact content-bound rule identities, validates emitted
documents and evidence, and publishes an immutable result atomically.
"""
