"""lib/scheduler/ — Task scheduler package.

Sub-modules:
  cron      — cron expression parser (parse_cron_expression, CronExpression)
  executor  — task execution handlers (run scheduled tasks)
  manager   — SchedulerManager class (add/remove/list/tick)
  tool_defs — tool schema definitions for scheduler-related tools
"""

import logging

from lib._pkg_utils import build_facade

_logger = logging.getLogger(__name__)

__all__: list[str] = []

# ── All modules (all required for scheduler to function) ─
from . import cron, executor, manager, proactive, timer, tool_defs  # noqa: E402
from .cron import *  # noqa: F401,F403
from .executor import *  # noqa: F401,F403
from .manager import *  # noqa: F401,F403
from .proactive import *  # noqa: F401,F403
from .timer import *  # noqa: F401,F403
from .tool_defs import *  # noqa: F401,F403

build_facade(__all__, cron, executor, manager, tool_defs, proactive, timer)
