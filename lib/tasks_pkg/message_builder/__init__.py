"""Message-building helpers — URL prefetch injection and tool-history restoration.

Extracted from ``orchestrator.py`` to isolate the logic that mutates the
``messages`` list before the main LLM tool loop begins.

This package was split from the former single-file
``lib/tasks_pkg/message_builder.py`` for readability. The public import
surface is unchanged, so every ``from lib.tasks_pkg.message_builder import X``
keeps working byte-identically:

  • :func:`inject_prefetched_urls`  — see ``_prefetch``
  • :func:`inject_tool_history`     — see ``_tool_history``
"""

from lib.log import get_logger

logger = get_logger(__name__)

from lib.tasks_pkg.message_builder._prefetch import (  # noqa: E402,F401
    inject_prefetched_urls,
)
from lib.tasks_pkg.message_builder._tool_history import (  # noqa: E402,F401
    PreparedContinueToolHistory,
    inject_tool_history,
    prepare_tool_history,
)

__all__ = [
    'inject_prefetched_urls',
    'inject_tool_history',
    'prepare_tool_history',
    'PreparedContinueToolHistory',
]
