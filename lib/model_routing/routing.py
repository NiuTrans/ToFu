"""Stable public routing exports; implementations live at their explicit owners.

Selection resolution builds authority indexes, candidate_compiler owns request
eligibility/ranking, and route_snapshot owns bounded diagnostics. Extend those
owners rather than adding behavior to this compatibility import surface.
"""

from .candidate_compiler import (
    RouteCandidate, RouteCandidateCompiler, RoutePolicy,
    compile_candidates, compile_model_fallback_candidates,
)
from .route_snapshot import RouteSnapshotBuilder, legacy_route_snapshot
from .selection_resolution import resolve_compatible_model


__all__ = [
    "RouteCandidate",
    "RouteCandidateCompiler",
    "RoutePolicy",
    "RouteSnapshotBuilder",
    "compile_candidates",
    "compile_model_fallback_candidates",
    "legacy_route_snapshot",
    "resolve_compatible_model",
]
