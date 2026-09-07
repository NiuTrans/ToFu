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
    local_serve,
    mcp,
    memory,
    motion_video,
    project,
    skills,
    tool_gateway,
)

# Miscellaneous handlers are split by capability; import the concrete owners
# explicitly so registration never depends on package-root side effects.
from lib.tasks_pkg.handlers.misc import (  # noqa: F401
    _agents as _misc_agents,
    _brain as _misc_brain,
    _human as _misc_human,
)

# Large tool-result continuation is a durable-storage capability.  The public
# ``tofu-agent`` wheel deliberately excludes ``lib.storage``; keep the common
# handler graph importable there while preserving fail-loud behavior for every
# unrelated import error.  The full application still registers these handlers
# because its declared storage package is present.
try:
    from lib.tasks_pkg.handlers import tool_result_artifacts  # noqa: F401
except ModuleNotFoundError as _artifact_storage_err:
    if not str(_artifact_storage_err.name or '').startswith('lib.storage'):
        raise

# Registration modules are dependency-light. The optional tofu-search graph is
# activated inside a handler only after its inputs pass validation, so every
# tool name remains discoverable and an unavailable search dependency produces
# a typed per-call failure instead of an opaque unknown-tool result.
from lib.tasks_pkg.handlers.search import _handlers as _search_handlers  # noqa: F401
from lib.tasks_pkg.handlers.search import _settings as _search_settings  # noqa: F401
