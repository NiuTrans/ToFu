"""Prompt-cache tracking namespace.

Concrete modules own cache state, break detection, ROI metrics, TTL latches,
prefix protection, and hashing. Import from that owner; the package root
intentionally exposes no mutable state or compatibility facade.
"""

__all__ = ()
