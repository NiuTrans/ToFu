"""Behavioral tests for the Feishu (Lark) bot's external-input surface.

Targets the two failure-prone, untrusted-input paths:
  * ``lib/feishu/events.py``   — webhook event parsing + routing
                                 (``handle_message_event`` / ``handle_menu_event``)
  * ``lib/feishu/commands.py`` — slash-command dispatch (``dispatch_command``)

These modules parse payloads that arrive from the network (the Lark SDK
dispatcher), so the contract that matters is: malformed / missing-field /
duplicate payloads must degrade GRACEFULLY — never raise out of the handler,
and emit a log/user-message instead.

No live Lark calls and no network: ``send_text`` and ``run_task_pipeline``
are stubbed, and the per-test ``_isolate_feishu`` fixture resets the module's
dedup cache + auth allow-list + per-user state so tests don't pollute each
other. The feishu sub-modules import cleanly with no server/DB/Quart-shim
dependency, so no ``import server`` ordering shim is needed here.
"""
from __future__ import annotations

import json
from types import SimpleNamespace
from unittest import mock

import pytest

from lib.feishu import commands, events

pytestmark = pytest.mark.unit


# ═══════════════════════════════════════════════════════════
#  Fixtures — isolate the module-level mutable state per test
# ═══════════════════════════════════════════════════════════

@pytest.fixture
def _isolate_feishu():
    """Reset the dedup cache and allow-list so tests are independent.

    Yields a dict of stub mocks for the collaborators ``events`` calls into
    (send_text / dispatch_command / run_task_pipeline / pending-state), each
    patched on the ``events`` module namespace where the name was bound.
    """
    events._processed_msgs.clear()
    with mock.patch.object(events, "ALLOWED_USERS", set()), \
            mock.patch.object(events, "send_text") as send_text, \
            mock.patch.object(events, "dispatch_command", return_value=None) as dispatch, \
            mock.patch.object(events, "run_task_pipeline", return_value="pipeline-reply") as pipeline, \
            mock.patch.object(events, "get_pending", return_value=None), \
            mock.patch.object(events, "clear_pending"):
        yield {
            "send_text": send_text,
            "dispatch_command": dispatch,
            "run_task_pipeline": pipeline,
        }


def _dict_message_event(text="hello", *, message_id="m1", open_id="ou_user", chat_id="oc_chat"):
    """Build a dict-form im.message.receive_v1 payload (the non-SDK path)."""
    return {
        "event": {
            "message": {
                "message_id": message_id,
                "message_type": "text",
                "content": json.dumps({"text": text}),
                "chat_id": chat_id,
            },
            "sender": {"sender_id": {"open_id": open_id}},
        }
    }


# ═══════════════════════════════════════════════════════════
#  1. handle_message_event — dict-form payload parsing + routing
# ═══════════════════════════════════════════════════════════

@pytest.mark.unit
class TestHandleMessageEvent:
    def test_regular_message_runs_pipeline(self, _isolate_feishu):
        events.handle_message_event(_dict_message_event("how are you"))
        _isolate_feishu["run_task_pipeline"].assert_called_once()
        # pipeline reply is sent back to the user
        _isolate_feishu["send_text"].assert_called_once()
        assert _isolate_feishu["send_text"].call_args.args[1] == "pipeline-reply"

    def test_slash_command_short_circuits_pipeline(self, _isolate_feishu):
        _isolate_feishu["dispatch_command"].return_value = "cmd-reply"
        events.handle_message_event(_dict_message_event("/status"))
        # command matched → pipeline NOT invoked
        _isolate_feishu["run_task_pipeline"].assert_not_called()
        assert _isolate_feishu["send_text"].call_args.args[1] == "cmd-reply"

    def test_empty_text_is_ignored(self, _isolate_feishu):
        events.handle_message_event(_dict_message_event(""))
        _isolate_feishu["run_task_pipeline"].assert_not_called()
        _isolate_feishu["send_text"].assert_not_called()

    def test_duplicate_message_skipped(self, _isolate_feishu):
        evt = _dict_message_event("hi", message_id="dup-1")
        events.handle_message_event(evt)
        events.handle_message_event(evt)  # same message_id again
        # pipeline ran exactly once despite two deliveries
        assert _isolate_feishu["run_task_pipeline"].call_count == 1

    def test_unauthorized_user_denied(self, _isolate_feishu):
        with mock.patch.object(events, "ALLOWED_USERS", {"ou_allowed"}):
            events.handle_message_event(
                _dict_message_event("hi", open_id="ou_stranger")
            )
        _isolate_feishu["run_task_pipeline"].assert_not_called()
        # a denial message is sent
        _isolate_feishu["send_text"].assert_called_once()

    def test_malformed_content_json_falls_back_to_raw_text(self, _isolate_feishu):
        evt = _dict_message_event("ignored")
        evt["event"]["message"]["content"] = "{not valid json"  # broken JSON
        events.handle_message_event(evt)
        # does not raise; treats the raw string as the message → pipeline runs
        _isolate_feishu["run_task_pipeline"].assert_called_once()
        passed_text = _isolate_feishu["run_task_pipeline"].call_args.args[1]
        assert passed_text == "{not valid json"

    def test_missing_message_field_does_not_raise(self, _isolate_feishu):
        # event with no 'message' key at all
        events.handle_message_event({"event": {"sender": {}}})
        _isolate_feishu["run_task_pipeline"].assert_not_called()
        _isolate_feishu["send_text"].assert_not_called()

    def test_completely_empty_payload_does_not_raise(self, _isolate_feishu):
        events.handle_message_event({})
        events.handle_message_event(None)  # type: ignore[arg-type]
        _isolate_feishu["run_task_pipeline"].assert_not_called()

    def test_sdk_object_form_payload(self, _isolate_feishu):
        """The SDK delivers an object with .event/.message attrs, not a dict."""
        sdk_event = SimpleNamespace(
            event=SimpleNamespace(
                message=SimpleNamespace(
                    message_id="sdk-1",
                    message_type="text",
                    content=json.dumps({"text": "via sdk"}),
                    chat_id="oc_sdk",
                ),
                sender=SimpleNamespace(
                    sender_id=SimpleNamespace(open_id="ou_sdk")
                ),
            )
        )
        events.handle_message_event(sdk_event)
        _isolate_feishu["run_task_pipeline"].assert_called_once()
        assert _isolate_feishu["run_task_pipeline"].call_args.args[1] == "via sdk"


# ═══════════════════════════════════════════════════════════
#  2. handle_menu_event — menu-key → command mapping
# ═══════════════════════════════════════════════════════════

@pytest.mark.unit
class TestHandleMenuEvent:
    def test_known_menu_key_maps_to_command(self, _isolate_feishu):
        _isolate_feishu["dispatch_command"].return_value = "help-text"
        events.handle_menu_event({
            "event": {
                "event_key": "help",
                "operator": {"operator_id": {"open_id": "ou_user"}},
                "chat_id": "oc_chat",
            }
        })
        # MENU_MAP['help'] == '/help' → dispatched
        _isolate_feishu["dispatch_command"].assert_called_once()
        assert _isolate_feishu["dispatch_command"].call_args.args[1] == "/help"

    def test_unknown_menu_key_falls_back_to_slash_key(self, _isolate_feishu):
        events.handle_menu_event({
            "event": {
                "event_key": "weird",
                "operator": {"operator_id": {"open_id": "ou_user"}},
            }
        })
        # unknown key → '/weird'
        assert _isolate_feishu["dispatch_command"].call_args.args[1] == "/weird"

    def test_empty_event_key_ignored(self, _isolate_feishu):
        events.handle_menu_event({"event": {"event_key": ""}})
        _isolate_feishu["dispatch_command"].assert_not_called()

    def test_malformed_menu_payload_does_not_raise(self, _isolate_feishu):
        events.handle_menu_event({})
        events.handle_menu_event(None)  # type: ignore[arg-type]
        _isolate_feishu["dispatch_command"].assert_not_called()

    def test_unauthorized_menu_user_denied(self, _isolate_feishu):
        with mock.patch.object(events, "ALLOWED_USERS", {"ou_allowed"}):
            events.handle_menu_event({
                "event": {
                    "event_key": "help",
                    "operator": {"operator_id": {"open_id": "ou_stranger"}},
                }
            })
        _isolate_feishu["dispatch_command"].assert_not_called()
        _isolate_feishu["send_text"].assert_called_once()


# ═══════════════════════════════════════════════════════════
#  3. dispatch_command — registry routing + graceful failure
# ═══════════════════════════════════════════════════════════

@pytest.mark.unit
class TestDispatchCommand:
    def test_non_command_returns_none(self):
        assert commands.dispatch_command("ou_user", "just a sentence") is None

    def test_empty_string_returns_none(self):
        assert commands.dispatch_command("ou_user", "   ") is None

    def test_help_command_matches(self):
        resp = commands.dispatch_command("ou_user", "/help")
        assert resp is not None and "/help" in resp

    def test_prefix_requires_word_boundary(self):
        """'/helpme' must NOT match the '/help' prefix (needs exact or space)."""
        assert commands.dispatch_command("ou_user", "/helpme") is None

    def test_command_with_argument(self):
        # _cmd_model with no DB dependency — just reads/writes module state
        resp = commands.dispatch_command("ou_user", "/model gpt-4o")
        assert resp is not None and "gpt-4o" in resp

    def test_handler_exception_is_caught_gracefully(self):
        """A handler that raises must yield an error string, not propagate."""
        boom = mock.Mock(side_effect=RuntimeError("kaboom"))
        with mock.patch.dict(commands.COMMAND_DISPATCH, {"/help": boom}):
            resp = commands.dispatch_command("ou_user", "/help")
        assert resp is not None
        assert "命令执行失败" in resp  # graceful failure marker, not a traceback

    def test_menu_map_targets_are_registered_commands(self):
        """Every MENU_MAP value must route to a real registered command."""
        for key, cmd_text in commands.MENU_MAP.items():
            prefix = cmd_text.split()[0]
            assert prefix in commands.COMMAND_DISPATCH, (
                f"MENU_MAP[{key!r}] -> {cmd_text!r} has no handler {prefix!r}"
            )
