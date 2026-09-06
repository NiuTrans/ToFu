"""lib.chat — chat-domain helpers that used to live in ``routes/chat.py``.

Relocated here (2026-06) to break the ``lib → routes`` circular-import
coupling: ``lib/message_queue`` reached UP into ``routes.chat`` for
``_append_user_msg_idempotent`` and ``_resolve_conv_refs``. Dependencies
now flow ``routes → lib`` only.

Import the owning submodule directly (``lib.chat.turn_builder``,
``lib.chat.messages``, ``lib.chat.persistence``); this package keeps no
re-export facade.
"""
