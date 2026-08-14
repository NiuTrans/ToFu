"""Compatibility alias for dependency-light :mod:`lib.paper_identity`."""

import sys as _sys
from lib import paper_identity as _impl

_sys.modules[__name__] = _impl
