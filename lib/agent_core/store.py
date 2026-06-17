"""lib/agent_core/store.py — Accessor for the persistence seam.

The reusable agent base reaches all persistence through a
:class:`~lib.protocols.ConversationStore`, obtained here via
:func:`get_conversation_store`.  This module is part of the agent base
(``lib.agent_core`` is in ``CORE_MODULES``), so it MUST NOT import
``lib.database`` / ``lib.conversations`` — it only names the *default
adapter module* (:mod:`lib.tasks_pkg.persistence_store`), which is itself
non-core and free to bind the DB.

Hosts that embed the agent base against a different persistence backend call
:func:`set_conversation_store` once at startup (mirroring how
``lib.search_bridge.install_search_bridge`` injects chatui behaviour into the
tofu-search seams).  When no override is installed, the chatui default
(:class:`lib.tasks_pkg.persistence_store.DefaultConversationStore`) is lazily
constructed on first use.
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING, Optional

from lib.log import get_logger

if TYPE_CHECKING:
    from lib.protocols import ConversationStore

logger = get_logger(__name__)

__all__ = ['get_conversation_store', 'set_conversation_store']

_store: Optional['ConversationStore'] = None
_lock = threading.Lock()


def set_conversation_store(store: 'ConversationStore') -> None:
    """Install the host's ConversationStore implementation.

    Call once at startup before any task runs.  Passing ``None`` resets to the
    lazily-constructed chatui default (useful in tests).
    """
    global _store
    with _lock:
        _store = store
    logger.info('[Store] conversation store set to %s',
                type(store).__name__ if store is not None else 'default (reset)')


def get_conversation_store() -> 'ConversationStore':
    """Return the active ConversationStore, constructing the default if unset.

    The default adapter is imported lazily so this core module never pulls in
    the DB layer at import time, and so a host can override before first use.
    """
    global _store
    if _store is not None:
        return _store
    with _lock:
        if _store is None:
            from lib.tasks_pkg.persistence_store import DefaultConversationStore
            _store = DefaultConversationStore()
            logger.debug('[Store] initialised default conversation store')
        return _store
