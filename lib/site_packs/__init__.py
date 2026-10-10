"""Site packs: declarative site internalization (docs/SITE_PACKS.md).

This package stays free of lib.browser imports at module level so lightweight
consumers (auth_sources, tool descriptions) can load the pack registry
without pulling in the browser stack. The adapter projection lives in
lib/site_packs/_projection.py and is imported lazily by the adapter registry.
"""

from ._loader import load_packs, pack_auth_catalog, reset_cache_for_tests
from ._manifest import ManifestError, PackManifest, PackOperation

__all__ = [
    'load_packs',
    'pack_auth_catalog',
    'reset_cache_for_tests',
    'ManifestError',
    'PackManifest',
    'PackOperation',
]
