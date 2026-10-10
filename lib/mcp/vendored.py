"""lib/mcp/vendored.py — registry of vendored internal MCP servers.

Deliberately TINY and dependency-free (stdlib only). Both the runtime
auto-installer (``lib/mcp/client.py``) and the release-time vendoring script
(``scripts/vendor_mcp.sh``) import ``VENDORED_LAUNCHERS`` from here, so the
list of internal servers lives in exactly ONE place. Keeping it in its own
module means the script can read the registry without importing the heavy
``client`` module (which pulls in asyncio, the MCP SDK, etc.).

Repository-owned entries declare ``first_party`` and resolve only their
``packages/`` source. They share the isolated runner; external vendoring
validates their presence without copying or replacing them.

Internal MCP servers (hope-mcp, …) are private and not on PyPI, so a fresh
Tofu checkout cannot obtain them — the user just sees "launcher X is not on
PATH". We ship the source next to the repo and launch it ISOLATED via
``uv run --no-project --with-editable <source>``: each server resolves its own
dependency tree (including its own ``mcp``) into its OWN environment, never
Tofu's interpreter. That decoupling is what lets the Tofu client and any
individual server move SDK versions independently (measured 2026-07-31: a v1
client and a v2 server interoperate on the wire, so the only thing that ever
made SDK versions couple was the shared interpreter).

``--with-editable`` (not ``uvx --from``) is deliberate: uv caches local-dir
wheel builds aggressively, and even ``--refresh`` / ``--reinstall`` were
measured to serve a STALE build (a file created in the source was absent from
the installed package). Editable links the source tree, so dev edits and
re-vendored snapshots are live on the next connect.

Registry shape — command → spec. ``sources`` is an ordered list of candidate
dirs (relative paths resolved lazily against :func:`repo_root`), tried in
order: a sibling dev checkout (``../<name>``, live edits — the developer
box), then an export-bundled copy (``vendor/<name>``), then the in-repo
vendored snapshot (``tools/<name>``, hermetic fallback). The first dir with a
``pyproject.toml`` wins.

To add a server: add one row here (and make sure either a sibling checkout or
a ``tools/<name>`` snapshot exists; ``make vendor-mcp`` populates the latter).
"""
from __future__ import annotations

import os
from typing import NotRequired, TypedDict

# Repo root = two dirs up from this file (lib/mcp/vendored.py → repo).
def repo_root() -> str:
    return os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))


class SourceLauncher(TypedDict):
    sources: list[str]
    first_party: NotRequired[bool]
    extra_sources: NotRequired[list[str]]


VENDORED_LAUNCHERS: dict[str, SourceLauncher] = {
    # Repository-owned source uses the same isolated runner. The vendoring
    # script must never replace it with a sibling checkout or a tools/ copy.
    'tofu-overleaf-mcp': {
        'sources': ['packages/tofu-overleaf-mcp'],
        'first_party': True,
    },
    'hope-mcp': {
        'extra_sources': ['packages/tofu-mtsso-auth'],
        'sources': ['../hope-mcp', 'vendor/hope-mcp', 'tools/hope-mcp'],
    },
    'llm-mcp': {
        'extra_sources': ['packages/tofu-mtsso-auth'],
        'sources': ['../llm-mcp', 'vendor/llm-mcp', 'tools/llm-mcp'],
    },
    'xuecheng-mcp': {
        'extra_sources': ['packages/tofu-mtsso-auth'],
        'sources': ['../xuecheng-mcp', 'vendor/xuecheng-mcp', 'tools/xuecheng-mcp'],
    },
}
