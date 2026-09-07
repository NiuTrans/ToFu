"""Terminal legacy tasks emit only a conversation wake hint.

The durable Conversation Sync v3 attempt event remains authoritative.  This
small seam exists for task flows that still mutate conversation metadata.
"""

from pathlib import Path

import pytest


pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[1]
FINALIZE_PATH = ROOT / "lib/tasks_pkg/orchestrator/_finalize.py"
FLOW_COMPLETION_PATH = ROOT / "lib/orchestration_chat_completion.py"
CALL = "notify_terminal_conversation_change(task)"


def test_terminal_seams_notify_after_the_done_event():
    root_source = FINALIZE_PATH.read_text(encoding="utf-8")
    assert root_source.index(CALL) > root_source.index(
        "append_event(task, done_evt)")

    flow_source = FLOW_COMPLETION_PATH.read_text(encoding="utf-8")
    assert flow_source.index("self._notify_terminal(self._task)") > \
        flow_source.index("self._append_event(self._task, done_event)")


def test_terminal_notification_is_owner_scoped(monkeypatch):
    import lib.tasks_pkg.manager._registry as _registry

    calls = []
    monkeypatch.setattr(
        "lib.conversations.notify_conv_changed",
        lambda conversation_id, **kwargs: calls.append((conversation_id, kwargs)),
    )

    _registry.notify_terminal_conversation_change(
        {"id": "task-1", "convId": "conversation-1", "_userId": "7"}
    )

    assert calls == [("conversation-1", {"rev": None, "user_id": 7})]


def test_terminal_notification_ignores_unbound_tasks(monkeypatch):
    import lib.tasks_pkg.manager._registry as _registry

    calls = []
    monkeypatch.setattr(
        "lib.conversations.notify_conv_changed",
        lambda *args, **kwargs: calls.append((args, kwargs)),
    )

    _registry.notify_terminal_conversation_change(None)
    _registry.notify_terminal_conversation_change({"id": "task-1", "convId": ""})

    assert calls == []
