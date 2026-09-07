"""Owner-scoped conversation-settings mutations over the storage authority.

Responsibility
--------------
Run arbitrary domain mutations against a fresh settings snapshot and commit
the complete result with a storage-side compare-and-swap. This single seam
supports additions, updates, and deletions without a process-local lock or a
legacy SQL fallback. A concurrent writer changes the compared snapshot, so the
mutation is retried against current state instead of silently losing keys.

Entry points
------------
``update_conversation_settings`` applies a callback transactionally.
``set_conversation_settings`` merges a flat mapping through that same path.

Dependencies
------------
Only the semantic storage client and the owner-scoped change notifier. SQLite
and PostgreSQL details remain inside the storage adapter.
"""

from __future__ import annotations

import copy
import hashlib
import json
from typing import Any, Callable

from lib.log import get_logger


logger = get_logger(__name__)
_MAX_CAS_ATTEMPTS = 8


def _publish_after_settings_write(
    conversation_id: str,
    user_id: int,
    notify: bool,
) -> None:
    """Publish UI-visible settings changes; internal settings stay silent."""
    if not notify:
        return
    try:
        from lib.conversations.change_notifications import notify_conv_changed

        notify_conv_changed(conversation_id, rev=None, user_id=user_id)
    except Exception as exc:
        logger.warning(
            "[SettingsStore] change notification failed conv=%s: %s",
            conversation_id[:8],
            exc,
        )


def _command_id(
    conversation_id: str,
    user_id: int,
    before: dict,
    after: dict,
) -> str:
    material = json.dumps(
        {"before": before, "after": after},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    digest = hashlib.sha256(material).hexdigest()
    return f"conversation-settings:{user_id}:{conversation_id}:{digest}"


def update_conversation_settings(
    conv_id: str,
    mutate: Callable[[dict], Any],
    *,
    user_id: int,
    notify: bool = True,
) -> dict | None:
    """Apply ``mutate`` to current settings with a storage-side snapshot CAS.

    The callback receives a private mutable copy. Returning ``False`` skips the
    write. ``None`` means the conversation does not exist. A conflict retries
    the callback against the newest snapshot, so callbacks must express a
    deterministic state transition and must not perform external side effects.
    """
    if not isinstance(conv_id, str) or not conv_id.strip():
        return None
    if isinstance(user_id, bool) or not isinstance(user_id, int) or user_id < 1:
        raise ValueError("user_id must be a positive integer")
    if not callable(mutate):
        raise TypeError("mutate must be callable")

    from lib.storage import get_storage_client

    client = get_storage_client(write=True)
    conversation_id = conv_id.strip()
    for _attempt in range(_MAX_CAS_ATTEMPTS):
        document = client.query(
            "conversation.get",
            {
                "conv_id": conversation_id,
                "user_id": user_id,
                "derive_messages": False,
            },
        )
        if not document:
            return None
        metadata = document.get("metadata") or {}
        raw_settings = metadata.get("settings") or {}
        if not isinstance(raw_settings, dict):
            raise RuntimeError("conversation settings projection is malformed")
        before = copy.deepcopy(raw_settings)
        after = copy.deepcopy(before)
        mutation_result = mutate(after)
        if mutation_result is False or after == before:
            return after

        result = client.command(
            "conversation.settings.update",
            {
                "conv_id": conversation_id,
                "user_id": user_id,
                "updates": after,
                "replace": True,
                "expected_settings": before,
            },
            _command_id(conversation_id, user_id, before, after),
        )
        if result.get("missing"):
            return None
        if result.get("applied"):
            _publish_after_settings_write(conversation_id, user_id, notify)
            return after

    logger.warning(
        "[SettingsStore] settings contention exceeded %d attempts conv=%s",
        _MAX_CAS_ATTEMPTS,
        conversation_id[:8],
    )
    return None


def set_conversation_settings(
    conv_id: str,
    updates: dict,
    *,
    user_id: int,
    notify: bool = True,
) -> dict | None:
    """Merge ``updates`` into one conversation's settings snapshot."""
    if not isinstance(updates, dict):
        raise TypeError("updates must be a dict")
    return update_conversation_settings(
        conv_id,
        lambda settings: settings.update(copy.deepcopy(updates)),
        user_id=user_id,
        notify=notify,
    )


__all__ = ["update_conversation_settings", "set_conversation_settings"]
