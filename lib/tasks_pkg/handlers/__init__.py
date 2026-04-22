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
    project,
    search,
)
