"""Small in-memory conversation authority for domain-level unit tests.

Tests patch the same repository/settings seams production services use. They
never emulate SQL rows, archived transcript blobs, or a storage-mode switch.
"""

from __future__ import annotations

import json

from lib.conversations.repository import ConversationSnapshot


def install_conversation_state(monkeypatch, state: dict) -> None:
    """Back repository reads and settings mutations with mutable ``state``.

    ``state['messages']`` and ``state['settings']`` may be decoded values or
    JSON strings. Writes preserve the original string representation expected
    by older assertions while exercising the current domain contracts.
    """

    def _decoded(key: str, default):
        value = state.get(key, default)
        if isinstance(value, str):
            return json.loads(value or ("[]" if key == "messages" else "{}"))
        return value

    def get_conversation(
        conversation_id: str,
        *,
        user_id: int,
        include_messages: bool = True,
    ):
        if state.get("missing"):
            return None
        settings = _decoded("settings", {})
        messages = _decoded("messages", []) if include_messages else []
        return ConversationSnapshot(
            metadata={
                "id": conversation_id,
                "user_id": user_id,
                "settings": settings,
                "rev": int(state.get("rev", 0)),
                "msg_count": len(messages),
            },
            messages=[dict(message) for message in messages],
        )

    def update_conversation_settings(
        _conversation_id: str,
        mutate,
        *,
        user_id: int,
        notify: bool = True,
    ):
        del user_id, notify
        if state.get("missing"):
            return None
        settings = _decoded("settings", {})
        result = mutate(settings)
        if result is not False:
            state["settings"] = json.dumps(settings, ensure_ascii=False)
        return settings

    import lib.conversations as conversations
    import lib.conversations.repository as repository

    monkeypatch.setattr(repository, "get_conversation", get_conversation)
    monkeypatch.setattr(
        conversations,
        "update_conversation_settings",
        update_conversation_settings,
    )


__all__ = ["install_conversation_state"]
