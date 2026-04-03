"""lib/llm_dispatch — Dynamic multi-key multi-model fastest-available LLM dispatcher.

Package structure:
  slot.py       — Slot dataclass (one api_key × model routing target)
  config.py     — Default slot configurations, model alias groups
  discovery.py  — Model auto-discovery via /v1/models + pricing enrichment
  dispatcher.py — LLMDispatcher class (slot pool management & selection)
  factory.py    — DispatcherFactory (Factory pattern), get_dispatcher(), reset_dispatcher()
  api.py        — High-level convenience functions (dispatch_chat, smart_chat, etc.)

All public names are re-exported here so that existing imports continue to work:
    from lib.llm_dispatch import dispatch_chat, smart_chat, get_dispatcher
"""

import logging

from lib._pkg_utils import build_facade

_logger = logging.getLogger(__name__)

__all__: list[str] = []

# ── All sub-modules (each guarded for resilience) ────────
try:
    from . import config  # noqa: E402
    from .config import *  # noqa: F401,F403
    build_facade(__all__, config)
except Exception as _exc:
    _logger.warning('lib.llm_dispatch.config failed to load: %s', _exc, exc_info=True)

try:
    from . import slot  # noqa: E402
    from .slot import *  # noqa: F401,F403
    build_facade(__all__, slot)
except Exception as _exc:
    _logger.warning('lib.llm_dispatch.slot failed to load: %s', _exc, exc_info=True)

try:
    from . import discovery  # noqa: E402
    from .discovery import *  # noqa: F401,F403
    build_facade(__all__, discovery)
except Exception as _exc:
    _logger.warning('lib.llm_dispatch.discovery failed to load: %s', _exc, exc_info=True)

try:
    from . import factory  # noqa: E402
    from .factory import *  # noqa: F401,F403
    build_facade(__all__, factory)
except Exception as _exc:
    _logger.warning('lib.llm_dispatch.factory failed to load: %s', _exc, exc_info=True)

try:
    from . import dispatcher  # noqa: E402
    from .dispatcher import *  # noqa: F401,F403
    build_facade(__all__, dispatcher)
except Exception as _exc:
    _logger.warning('lib.llm_dispatch.dispatcher failed to load: %s', _exc, exc_info=True)

try:
    from . import api  # noqa: E402
    from .api import *  # noqa: F401,F403
    build_facade(__all__, api)
except Exception as _exc:
    _logger.warning('lib.llm_dispatch.api failed to load: %s', _exc, exc_info=True)
