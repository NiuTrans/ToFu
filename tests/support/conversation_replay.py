"""Finite replay storage double shared by single and multiplexed stream tests.

ScriptedConversationRepository advances pages and notifies the supplied real
broker. Routes and ConversationSyncService retain their production behavior.
"""


class ScriptedConversationRepository:
    """Per-conversation pages; an exhausted script expires the cursor.

    Advancing a page invalidates its scope like the authority watch. Heartbeats
    carry liveness only and cannot reveal the script's next state.
    ``None`` is an absent conversation. An exception fails the first page read
    after the ownership probe, as a storage failure in a live stream does.
    """

    def __init__(self, scripts: dict, broker):
        self.broker = broker
        self.scripts = {
            key: list(value) if isinstance(value, list) else value
            for key, value in scripts.items()
        }

    def changes(self, conversation_id, user_id, *, after_sequence, limit):
        script = self.scripts.get(conversation_id)
        if script is None:
            return None
        if limit == 1:
            return {"head": after_sequence, "events": [],
                    "resetRequired": False, "hasMore": False}
        if isinstance(script, Exception):
            raise script
        if script:
            page = script.pop(0)
            self.broker.invalidate([{"userId": user_id, "conversationId": conversation_id}])
            return page
        return {"head": 0, "events": [], "resetRequired": True,
                "resetReason": "cursor_expired"}
