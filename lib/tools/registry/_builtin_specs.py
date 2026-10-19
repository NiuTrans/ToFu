"""Compatibility import for the canonical built-in spec factory.

The runtime owner is _build.build_builtin_specs; no test-only mirror exists.
"""
from ._build import build_builtin_specs

__all__ = ['build_builtin_specs']
