"""Compatibility alias for :mod:`lib.paper_hash_backfill`.

The implementation lives outside the eager ``lib.paper`` package so database
maintenance does not initialize PDF/ONNX/LLM runtimes. Existing imports keep
the same module object, including monkeypatchable migration paths/constants.
"""

import sys as _sys
from lib import paper_hash_backfill as _impl

_sys.modules[__name__] = _impl
