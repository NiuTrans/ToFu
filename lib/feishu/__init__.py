"""Feishu integration namespace.

Runtime state, conversation persistence, messaging, pipeline, event handling,
and startup each live in their named module; ``authority`` is the single
sender admission/authorization boundary.  Optional integration failures
are reported by the production lifecycle boundary, not swallowed at import.
"""

__all__ = ()
