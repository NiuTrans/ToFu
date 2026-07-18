# HOT_PATH
"""Miscellaneous tool handlers: ask_human, scheduler, desktop, swarm, conv_ref,
charter, board and peer families.

This package preserves the ``lib.tasks_pkg.handlers.misc`` module facade after
a split into cohesive submodules:

  • ``_human``  — ``_handle_ask_human``, ``_handle_todo_write``
  • ``_agents`` — ``_handle_scheduler_tool``, ``_run_desktop``,
                  ``_handle_desktop_tool``, ``_handle_swarm_tool``,
                  ``_build_await_post_build``
  • ``_brain``  — ``_handle_conv_ref_tool``, ``_handle_charter_tool``,
                  ``_handle_board_tool``, ``_make_intervention_approval_fn``,
                  ``_handle_peer_tool``

MONKEYPATCH PARITY: the collaborators ``append_event`` (and, for the
``ask_human`` path, ``_build_simple_meta`` / ``_finalize_tool_round``) are
imported INTO this package module and the submodule handlers resolve them
THROUGH this facade at call time (via
``from lib.tasks_pkg.handlers import misc as _facade; _facade.<name>(...)``).
That keeps ``monkeypatch.setattr('lib.tasks_pkg.handlers.misc.<name>', …)``
steering the handlers exactly as it did before the split (see
test_project_peer.py / test_project_feed_read_tool.py / test_tool_audit_tranche1.py).
"""

from __future__ import annotations

import os

from lib.log import get_logger

# ── Patch targets: imported INTO this package module so tests can steer them
#    via ``lib.tasks_pkg.handlers.misc.<name>``. Submodule handlers read these
#    back through THIS module at call time (facade indirection), preserving
#    monkeypatch parity with the pre-split single-module layout. ──
from lib.tasks_pkg.executor import (  # noqa: F401
    _build_simple_meta,
    _finalize_tool_round,
)
from lib.tasks_pkg.manager import append_event  # noqa: F401

logger = get_logger(__name__)


# ── Shared constant: application root (repo root — four dirnames up from this
#    package's __init__.py: misc/ → handlers/ → tasks_pkg/ → lib/ → repo). ──
_APP_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))


# ── Re-export every handler symbol so the module facade is byte-compatible.
#    Importing the submodules also triggers their @tool_registry registrations. ──
from lib.tasks_pkg.handlers.misc._human import (  # noqa: E402,F401
    _handle_ask_human,
    _handle_todo_write,
)
from lib.tasks_pkg.handlers.misc._agents import (  # noqa: E402,F401
    _SWARM_BADGE_VERB,
    _build_await_post_build,
    _handle_desktop_tool,
    _handle_scheduler_tool,
    _handle_swarm_tool,
    _run_desktop,
)
from lib.tasks_pkg.handlers.misc._brain import (  # noqa: E402,F401
    _handle_board_tool,
    _handle_charter_tool,
    _handle_conv_ref_tool,
    _handle_peer_tool,
    _make_intervention_approval_fn,
)


__all__ = [
    'append_event',
    '_build_simple_meta',
    '_finalize_tool_round',
    '_APP_ROOT',
    # _human
    '_handle_ask_human',
    '_handle_todo_write',
    # _agents
    '_SWARM_BADGE_VERB',
    '_build_await_post_build',
    '_handle_desktop_tool',
    '_handle_scheduler_tool',
    '_handle_swarm_tool',
    '_run_desktop',
    # _brain
    '_handle_board_tool',
    '_handle_charter_tool',
    '_handle_conv_ref_tool',
    '_handle_peer_tool',
    '_make_intervention_approval_fn',
]
