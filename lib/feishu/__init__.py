"""lib/feishu/ — Feishu (Lark) Bot integration package.

Decomposed from the monolithic lib/feishu_bot.py (1150 lines) into focused sub-modules:
  _state.py       — Per-user state, locks, configuration constants
  conversation.py — Chat history, DB sync, model/mode management
  messaging.py    — Lark API message sending, chunking
  pipeline.py     — Unified LLM task pipeline
  commands.py     — Slash command registry and handlers
  events.py       — Feishu event handlers (message, menu)
  startup.py      — WebSocket connection and reconnection

Public API (backward-compatible with ``from lib.feishu_bot import ...``):
"""

import logging

from lib._pkg_utils import build_facade

_logger = logging.getLogger(__name__)

# ── Aggregated __all__ ───────────────────────────────────
__all__: list[str] = []

# ── Core (must load — let exceptions propagate) ──────────
from . import _state  # noqa: E402
from ._state import *  # noqa: F401,F403

build_facade(__all__, _state)

# ── Feature modules (graceful degradation) ───────────────
# Each sub-module is imported with try/except so a broken dependency
# in one doesn't prevent the rest from loading.  Explicit `from .X import *`
# keeps static analysis / IDE autocomplete working.

try:
    from . import conversation  # noqa: E402
    from .conversation import *  # noqa: F401,F403
    build_facade(__all__, conversation)
except Exception as _exc:
    _logger.warning('lib.feishu.conversation failed to load — chat history disabled: %s', _exc, exc_info=True)

try:
    from . import messaging  # noqa: E402
    from .messaging import *  # noqa: F401,F403
    build_facade(__all__, messaging)
except Exception as _exc:
    _logger.warning('lib.feishu.messaging failed to load — message sending disabled: %s', _exc, exc_info=True)

try:
    from . import pipeline  # noqa: E402
    from .pipeline import *  # noqa: F401,F403
    build_facade(__all__, pipeline)
except Exception as _exc:
    _logger.warning('lib.feishu.pipeline failed to load — LLM pipeline disabled: %s', _exc, exc_info=True)

try:
    from . import commands  # noqa: E402
    from .commands import *  # noqa: F401,F403
    build_facade(__all__, commands)
except Exception as _exc:
    _logger.warning('lib.feishu.commands failed to load — slash commands disabled: %s', _exc, exc_info=True)

try:
    from . import events  # noqa: E402
    from .events import *  # noqa: F401,F403
    build_facade(__all__, events)
except Exception as _exc:
    _logger.warning('lib.feishu.events failed to load — event handlers disabled: %s', _exc, exc_info=True)

try:
    from . import startup  # noqa: E402
    from .startup import *  # noqa: F401,F403
    build_facade(__all__, startup)
except Exception as _exc:
    _logger.warning('lib.feishu.startup failed to load — WebSocket connection disabled: %s', _exc, exc_info=True)
