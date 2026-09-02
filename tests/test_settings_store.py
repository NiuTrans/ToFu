"""Settings mutations use one owner-scoped, storage-side CAS boundary."""

from __future__ import annotations

import copy
import threading

import pytest


pytestmark = pytest.mark.unit


class _SettingsClient:
    """Protocol-faithful in-memory client with deterministic conflict injection."""

    def __init__(self, settings: dict | None = None, *, missing: bool = False):
        self.settings = copy.deepcopy(settings or {})
        self.missing = missing
        self.commands: list[tuple] = []
        self.lock = threading.Lock()
        self.inject_before_next_command = None

    def query(self, operation, payload):
        assert operation == "conversation.get"
        assert payload["derive_messages"] is False
        if self.missing:
            return None
        with self.lock:
            settings = copy.deepcopy(self.settings)
        return {
            "metadata": {
                "id": payload["conv_id"],
                "user_id": payload["user_id"],
                "settings": settings,
                "rev": 7,
            },
            "messages": [],
        }

    def command(self, operation, payload, command_id):
        assert operation == "conversation.settings.update"
        assert payload["replace"] is True
        with self.lock:
            if self.inject_before_next_command is not None:
                injection = self.inject_before_next_command
                self.inject_before_next_command = None
                injection(self.settings)
            self.commands.append((copy.deepcopy(payload), command_id))
            if self.settings != payload["expected_settings"]:
                return {
                    "applied": False,
                    "missing": False,
                    "conflict": True,
                    "rev": 7,
                }
            self.settings = copy.deepcopy(payload["updates"])
            return {
                "applied": True,
                "missing": False,
                "conflict": False,
                "rev": 7,
            }


@pytest.fixture
def install_client(monkeypatch):
    notifications = []

    def install(client):
        monkeypatch.setattr(
            "lib.storage.get_storage_client", lambda **_kwargs: client
        )
        monkeypatch.setattr(
            "lib.conversations.settings_store._publish_after_settings_write",
            lambda *args: notifications.append(args),
        )
        return notifications

    return install


def test_set_calls_merge_without_clobbering_unrelated_keys(install_client):
    from lib.conversations import set_conversation_settings

    client = _SettingsClient({"model": "x"})
    notifications = install_client(client)

    set_conversation_settings("conv-settings", {"activeTaskId": "t1"}, user_id=1)
    set_conversation_settings("conv-settings", {"autopilotEnabled": True}, user_id=1)

    assert client.settings == {
        "model": "x",
        "activeTaskId": "t1",
        "autopilotEnabled": True,
    }
    assert len(notifications) == 2


def test_callback_can_delete_keys(install_client):
    from lib.conversations import update_conversation_settings

    client = _SettingsClient({"autopilotRunId": "run-1", "model": "x"})
    install_client(client)

    result = update_conversation_settings(
        "conv-settings",
        lambda settings: settings.pop("autopilotRunId"),
        user_id=1,
    )

    assert result == {"model": "x"}
    assert client.settings == {"model": "x"}
    payload, _command_id = client.commands[-1]
    assert payload["updates"] == {"model": "x"}
    assert payload["expected_settings"]["autopilotRunId"] == "run-1"


def test_conflict_replays_mutation_against_fresh_snapshot(install_client):
    from lib.conversations import update_conversation_settings

    client = _SettingsClient({"counter": 0})
    client.inject_before_next_command = lambda settings: settings.update(
        {"counter": 1, "concurrentKey": "preserved"}
    )
    install_client(client)

    calls = 0

    def increment(settings):
        nonlocal calls
        calls += 1
        settings["counter"] = int(settings.get("counter", 0)) + 1

    result = update_conversation_settings("conv-settings", increment, user_id=1)

    assert calls == 2
    assert result == {"counter": 2, "concurrentKey": "preserved"}
    assert client.settings == result
    assert len(client.commands) == 2
    assert client.commands[0][1] != client.commands[1][1]


def test_false_or_unchanged_mutation_skips_command_and_notification(
    install_client,
):
    from lib.conversations import update_conversation_settings

    client = _SettingsClient({"model": "x"})
    notifications = install_client(client)

    result = update_conversation_settings(
        "conv-settings",
        lambda settings: settings.update({"temporary": True}) or False,
        user_id=1,
    )
    unchanged = update_conversation_settings(
        "conv-settings",
        lambda settings: settings.update({"model": "x"}),
        user_id=1,
    )

    assert result["temporary"] is True
    assert unchanged == {"model": "x"}
    assert client.settings == {"model": "x"}
    assert client.commands == []
    assert notifications == []


def test_missing_conversation_returns_none(install_client):
    from lib.conversations import set_conversation_settings

    client = _SettingsClient(missing=True)
    notifications = install_client(client)

    assert set_conversation_settings("missing", {"a": 1}, user_id=1) is None
    assert client.commands == []
    assert notifications == []


def test_owner_is_explicit_in_query_and_command(install_client):
    from lib.conversations import set_conversation_settings

    client = _SettingsClient()
    install_client(client)

    set_conversation_settings("conv-settings", {"a": 1}, user_id=47)

    payload, command_id = client.commands[-1]
    assert payload["user_id"] == 47
    assert command_id.startswith("conversation-settings:47:conv-settings:")


def test_owner_is_a_required_core_argument():
    from lib.conversations import (
        set_conversation_settings,
        update_conversation_settings,
    )

    with pytest.raises(TypeError, match="user_id"):
        set_conversation_settings("conv-settings", {"a": 1})
    with pytest.raises(TypeError, match="user_id"):
        update_conversation_settings("conv-settings", lambda settings: None)


def test_same_conversation_id_cannot_cross_owner_boundary(
    install_client,
):
    from lib.conversations import set_conversation_settings

    owner_clients = {
        11: _SettingsClient({"owner": 11, "value": "left"}),
        22: _SettingsClient({"owner": 22, "value": "right"}),
    }

    class _OwnerRouter:
        def query(self, operation, payload):
            return owner_clients[payload["user_id"]].query(operation, payload)

        def command(self, operation, payload, command_id):
            return owner_clients[payload["user_id"]].command(
                operation, payload, command_id)

    install_client(_OwnerRouter())
    set_conversation_settings(
        "shared-conversation-id", {"value": "updated"}, user_id=11)

    assert owner_clients[11].settings["value"] == "updated"
    assert owner_clients[22].settings == {"owner": 22, "value": "right"}
