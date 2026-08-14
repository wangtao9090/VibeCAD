"""Reviewed planar-mechanical v1 visual-to-intent rule pack.

Composition helpers intentionally live in :mod:`.catalog`.  Keeping this
package initializer limited to the pure rule prevents a concrete source
adapter/catalog import cycle.
"""

from .rule_set import (
    CircleProfile,
    PlanarMechanicalEvidence,
    PlanarMechanicalV1RuleSet,
    RotatedRectangle,
    analyze_visual_feature_graph,
    build_intent_graphs,
)

__all__ = [
    "CircleProfile",
    "PlanarMechanicalEvidence",
    "PlanarMechanicalV1RuleSet",
    "RotatedRectangle",
    "analyze_visual_feature_graph",
    "build_intent_graphs",
]
