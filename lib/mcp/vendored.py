"""lib/mcp/vendored.py — registry of vendored internal MCP servers.

Deliberately TINY and dependency-free (stdlib only). Both the runtime
auto-installer (``lib/mcp/client.py``) and the release-time vendoring script
(``scripts/vendor_mcp.sh``) import ``VENDORED_LAUNCHERS`` from here, so the
list of internal servers lives in exactly ONE place. Keeping it in its own
module means the script can read the registry without importing the heavy
``client`` module (which pulls in asyncio, the MCP SDK, etc.).

Internal MCP servers (hope-mcp, …) are private and not on PyPI, so a fresh
Tofu checkout cannot obtain them — the user just sees "launcher X is not on
PATH". We ship the source in-repo and pip-install it into Tofu's own
interpreter on first connect.

Registry shape — command → spec. ``sources`` is an ordered list of candidate
dirs (relative paths resolved lazily against :func:`repo_root`). The in-repo
vendored snapshot lives under ``tools/<name>`` (always present on a fresh
checkout); a sibling dev checkout (``../<name>``) is preferred when present.

Editable-ness is decided PER SOURCE, not per command (see
``client._find_vendored_source``): the sibling dev checkout is installed
editable so live edits are tracked on a developer box, while the vendored
snapshot under ``tools/`` is installed NON-editable so a deploy gets a
hermetic copy that doesn't depend on the source tree staying in place.

To add a server: add one row here (and make sure either a sibling checkout or
a ``tools/<name>`` snapshot exists; ``make vendor-mcp`` populates the latter).
"""
from __future__ import annotations

import os

# Repo root = two dirs up from this file (lib/mcp/vendored.py → repo).
def repo_root() -> str:
    return os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))


VENDORED_LAUNCHERS: dict[str, dict[str, list[str]]] = {
    'hope-mcp': {
        'sources': ['../hope-mcp', 'tools/hope-mcp'],
    },
    'llm-mcp': {
        'sources': ['../llm-mcp', 'tools/llm-mcp'],
    },
}
