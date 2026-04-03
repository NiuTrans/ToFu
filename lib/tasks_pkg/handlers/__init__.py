"""lib.tasks_pkg.handlers — Tool handler submodules.

Importing this package triggers registration of all tool handlers
on the :data:`~lib.tasks_pkg.executor.tool_registry` singleton.

Each submodule uses the ``@tool_registry.handler()`` / ``@tool_registry.tool_set()``
/ ``@tool_registry.special()`` decorators, so handlers are registered at import time
(same pattern as Flask Blueprints).
"""

# Import all handler modules to trigger their @tool_registry registrations.
# Order doesn't matter — each module registers independently.
from lib.tasks_pkg.handlers import (  # noqa: F401
    browser,
    code_exec,
    misc,
    project,
    search,
    skills,
)
