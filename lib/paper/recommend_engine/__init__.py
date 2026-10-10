"""Recommendation-engine namespace.

``_research`` owns model/tool research, ``_ground`` owns the arXiv truth gate,
and ``_events`` owns streaming plus its blocking projection.  Import the owning
module directly; the package keeps no mutable facade state.
"""

__all__: tuple[str, ...] = ()
