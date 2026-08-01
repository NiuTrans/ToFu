"""lib.tasks_pkg.handlers — Tool handler submodules.

Importing this package triggers registration of all tool handlers
on the :data:`~lib.tasks_pkg.executor.tool_registry` singleton.

Each submodule uses the ``@tool_registry.handler()`` / ``@tool_registry.tool_set()``
/ ``@tool_registry.special()`` decorators, so handlers are registered at import time
(same pattern as Flask Blueprints).

Shared DRY primitives live in ``_adapter.py`` (``simple_call``,
``run_batch_concurrent``) and are used by multiple handler modules.
"""

# Import all handler modules to trigger their @tool_registry registrations.
# Order doesn't matter — each module registers independently.
from lib.tasks_pkg.handlers import (  # noqa: F401
    browser,
    code_exec,
    mcp,
    memory,
    misc,
    motion_video,
    project,
    skills,
)

# ``search`` is imported SEPARATELY and degradably. Its three modules import
# tofu_search at module level, which pulls trafilatura → lxml → libicuuc; on
# 2026-07-31 that chain raised
#   ImportError: /lib64/libstdc++.so.6: version `GLIBCXX_3.4.30' not found
# eight times. This package sits on the boot chain, so a bare import made a
# fault confined to WEB SEARCH kill the entire server — chat, projects, the
# scheduler and everything else died with it.
#
# Degrading here means the web_search / fetch_url handlers are simply not
# registered: calling them returns an unknown-tool error while every other
# tool and subsystem works normally. That is the correct blast radius for one
# optional capability. Guarded at the package seam rather than per-symbol
# because all three modules under handlers/search/ share the same dependency.
try:
    from lib.tasks_pkg.handlers import search  # noqa: F401
except ImportError as _search_err:
    from lib.log import get_logger as _get_logger
    _get_logger(__name__).error(
        'Web search/fetch handlers are NOT registered — the search handler '
        'package could not be imported: %s. web_search/fetch_url will report '
        'an unknown tool; all other tools are unaffected.',
        _search_err, exc_info=True)
