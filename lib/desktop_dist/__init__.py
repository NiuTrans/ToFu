"""lib/desktop_dist — server-hosted desktop-installer distribution.

Why this package exists
-----------------------
The desktop download used to be a link to GitHub Releases, and the status
endpoint computed per-platform links by probing ``api.github.com``
SYNCHRONOUSLY inside an async route (up to 6 s on the event loop every
cache expiry — the measurable stall behind "this element always takes much
longer to load"). Both the probe and the client's own fetch also depended
on the public GitHub network being reachable and fast, which is not a
given from either side.

This package makes the SERVER the download origin:

  platforms — which installer does this visitor need (UA/arch narrowing,
              release-payload parsing). Extracted from
              ``routes/api_v1/desktop.py``; the route re-exports the names
              so existing guards see no drift.
  store     — the on-disk artifact store + manifest (traversal-proof
              resolution, stale-while-revalidate, built-beats-mirrored
              version preference).
  mirror    — background refresh of the published GitHub assets (the
              platforms this server cannot build itself).
  builder   — native on-server build for the platform the server CAN
              build (its own), wired in a later slice.

The request path performs ZERO network: it reads the store. All network
happens in the mirror's single-flight background thread.
"""

from lib.desktop_dist import mirror, platforms, store

__all__ = ['platforms', 'store', 'mirror']
