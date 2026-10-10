"""MCP server exposing tofu-search over the Model Context Protocol.

Install the optional dependency group and run the entry point::

    pip install "tofu-search[mcp]"
    tofu-search-mcp                      # stdio (the default)
    tofu-search-mcp --transport http     # Streamable HTTP

``python -m tofu_search.mcp_server`` runs exactly the same ``main()``.

Named ``mcp_server``, not ``mcp``: this package ALSO consumes external MCP
servers (see ``tofu_search.search.vertical``), so an unqualified ``mcp`` would
be ambiguous about direction -- and a top-level ``tofu_search.mcp`` submodule
risks shadowing the third-party ``mcp`` SDK it has to import.

The import of :func:`build_server` is deliberately NOT re-exported here: the
SDK is an optional dependency, so importing this package must not fail when it
is absent. Import ``tofu_search.mcp_server.server`` explicitly.
"""

__all__ = ['main']


def main(argv: list[str] | None = None) -> int:
    """Entry point; imported lazily so ``-h`` works without the SDK installed."""
    from tofu_search.mcp_server.__main__ import main as _main

    return _main(argv)
