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

import asyncio
import json
import sys
from types import SimpleNamespace
from unittest import mock

import pytest

import lib.feishu.commands as commands
import lib.feishu.events as events
import lib.feishu.messaging as messaging
import lib.feishu.pipeline as pipeline

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


def test_project_tool_failure_does_not_expose_internal_error():
    secret = 'secret-tool-path-and-provider-detail'
    with mock.patch(
        'lib.project_mod.tools.execute_tool',
        side_effect=RuntimeError(secret),
    ):
        response = pipeline.exec_project_tool('ou-tool-error', 'read_file', {})
    assert response == '❌ 工具执行失败，请稍后重试'
    assert secret not in response


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

    def test_internal_exception_is_logged_but_not_sent_to_user(
        self, _isolate_feishu,
    ):
        secret = 'provider-secret-and-internal-path'
        _isolate_feishu['run_task_pipeline'].side_effect = RuntimeError(secret)

        events.handle_message_event(_dict_message_event(
            'trigger failure', message_id='redaction-1'))

        user_message = _isolate_feishu['send_text'].call_args.args[1]
        assert user_message == '❌ 请求处理失败，请稍后重试'
        assert secret not in user_message


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
        secret = 'secret-command-detail'
        boom = mock.Mock(side_effect=RuntimeError(secret))
        with mock.patch.dict(commands.COMMAND_DISPATCH, {"/help": boom}):
            resp = commands.dispatch_command("ou_user", "/help")
        assert resp is not None
        assert "命令执行失败" in resp  # graceful failure marker, not a traceback
        assert secret not in resp

    def test_menu_map_targets_are_registered_commands(self):
        """Every MENU_MAP value must route to a real registered command."""
        for key, cmd_text in commands.MENU_MAP.items():
            prefix = cmd_text.split()[0]
            assert prefix in commands.COMMAND_DISPATCH, (
                f"MENU_MAP[{key!r}] -> {cmd_text!r} has no handler {prefix!r}"
            )


# ═══════════════════════════════════════════════════════════
#  4. GUI-saved config — apply_config, live creds, boot path
# ═══════════════════════════════════════════════════════════

@pytest.fixture
def _preserve_feishu_state():
    """Snapshot/restore _state scalars, allow-list, and the client singleton."""
    import lib.feishu._state as st
    snap = dict(app_id=st.APP_ID, app_secret=st.APP_SECRET, enabled=st.ENABLED,
                allowed=set(st.ALLOWED_USERS), default=st.DEFAULT_PROJECT_PATH,
                root=st.WORKSPACE_ROOT, client=st._lark_client)
    yield st
    st.APP_ID = snap['app_id']
    st.APP_SECRET = snap['app_secret']
    st.ENABLED = snap['enabled']
    st.ALLOWED_USERS.clear()
    st.ALLOWED_USERS.update(snap['allowed'])
    st.DEFAULT_PROJECT_PATH = snap['default']
    st.WORKSPACE_ROOT = snap['root']
    st._lark_client = snap['client']


class TestApplyConfig:
    def test_credential_change_resets_client_and_enables(self, _preserve_feishu_state):
        st = _preserve_feishu_state
        st._lark_client = object()
        changed = st.apply_config({
            'app_id': 'cli_new', 'app_secret': 'sec_new',
            'allowed_users': ['ou_a'], 'default_project': '/tmp/p',
            'workspace_root': '/tmp/r',
        })
        assert changed is True
        assert (st.APP_ID, st.APP_SECRET, st.ENABLED) == ('cli_new', 'sec_new', True)
        assert st._lark_client is None  # singleton forced to rebuild with new creds
        assert st.ALLOWED_USERS == {'ou_a'}
        assert st.DEFAULT_PROJECT_PATH == '/tmp/p'
        assert st.WORKSPACE_ROOT == '/tmp/r'

    def test_allowed_users_set_is_mutated_in_place(self, _preserve_feishu_state):
        """By-reference importers (events/commands) must observe GUI updates."""
        st = _preserve_feishu_state
        ref = st.ALLOWED_USERS
        st.apply_config({'allowed_users': ['ou_gui']})
        assert ref is st.ALLOWED_USERS
        assert 'ou_gui' in ref

    def test_default_project_reader_observes_live_config(
        self, _preserve_feishu_state,
    ):
        from lib.feishu.conversation import get_project

        st = _preserve_feishu_state
        st.apply_config({'default_project': '/tmp/live-feishu-project'})
        assert get_project('ou-live-config') == '/tmp/live-feishu-project'

    def test_unchanged_credentials_keep_client(self, _preserve_feishu_state):
        st = _preserve_feishu_state
        st.APP_ID, st.APP_SECRET = 'cli_same', 'sec_same'
        sentinel = object()
        st._lark_client = sentinel
        assert st.apply_config({'app_id': 'cli_same', 'app_secret': 'sec_same'}) is False
        assert st._lark_client is sentinel

    def test_empty_secret_disables(self, _preserve_feishu_state):
        st = _preserve_feishu_state
        st.APP_ID, st.APP_SECRET = 'cli_x', 'sec_x'
        st.apply_config({'app_secret': ''})
        assert st.APP_SECRET == ''
        assert st.ENABLED is False

    def test_absent_keys_are_left_untouched(self, _preserve_feishu_state):
        st = _preserve_feishu_state
        st.APP_ID, st.APP_SECRET = 'cli_keep', 'sec_keep'
        before_paths = (st.DEFAULT_PROJECT_PATH, st.WORKSPACE_ROOT)
        assert st.apply_config({'allowed_users': []}) is False
        assert (st.APP_ID, st.APP_SECRET) == ('cli_keep', 'sec_keep')
        assert (st.DEFAULT_PROJECT_PATH, st.WORKSPACE_ROOT) == before_paths


def _fake_lark_oapi(record):
    """Minimal lark_oapi stand-in capturing the builder's credentials."""
    import types

    class _Builder:
        def __init__(self):
            self._kw = {}

        def app_id(self, v):
            self._kw['app_id'] = v
            return self

        def app_secret(self, v):
            self._kw['app_secret'] = v
            return self

        def log_level(self, _v):
            return self

        def build(self):
            record.update(self._kw)
            return object()

    mod = types.ModuleType('lark_oapi')
    mod.Client = types.SimpleNamespace(builder=_Builder)
    mod.LogLevel = types.SimpleNamespace(WARNING=30)
    return mod


class TestMessagingLiveCredentials:
    def test_client_rebuild_uses_live_state_credentials(self, monkeypatch,
                                                        _preserve_feishu_state):
        st = _preserve_feishu_state
        st.APP_ID, st.APP_SECRET, st._lark_client = 'cli_live', 'sec_live', None
        record = {}
        monkeypatch.setitem(sys.modules, 'lark_oapi', _fake_lark_oapi(record))
        client = messaging._get_lark_client()
        assert record == {'app_id': 'cli_live', 'app_secret': 'sec_live'}
        assert client is st._lark_client  # singleton cached in _state


class _FakeThread:
    """threading.Thread stand-in: never runs the target, tracks liveness."""
    instances = []

    def __init__(self, target=None, daemon=None, name=None):
        self.target, self.daemon, self.name = target, daemon, name
        self._alive = False
        _FakeThread.instances.append(self)

    def start(self):
        self._alive = True

    def is_alive(self):
        return self._alive


@pytest.fixture
def _startup(monkeypatch, _preserve_feishu_state):
    import threading

    import lib.feishu.startup as startup
    _FakeThread.instances.clear()
    monkeypatch.setattr(threading, 'Thread', _FakeThread)
    monkeypatch.setattr(startup, '_bot_thread', None)
    return startup


class TestStartupLifecycle:
    def test_disabled_without_credentials(self, _startup, _preserve_feishu_state):
        _preserve_feishu_state.ENABLED = False
        assert _startup.start_bot() is False
        assert _FakeThread.instances == []
        assert _startup.is_bot_running() is False

    def test_start_spawns_tracked_daemon_thread(self, _startup, _preserve_feishu_state):
        _preserve_feishu_state.ENABLED = True
        assert _startup.start_bot() is True
        (t,) = _FakeThread.instances
        assert t.daemon is True and t.is_alive()
        assert _startup.is_bot_running() is True

    def test_second_start_is_a_noop(self, _startup, _preserve_feishu_state):
        _preserve_feishu_state.ENABLED = True
        assert _startup.start_bot() is True
        assert _startup.start_bot() is True
        assert len(_FakeThread.instances) == 1  # never two WS connections

    def test_restart_after_thread_death(self, _startup, _preserve_feishu_state):
        _preserve_feishu_state.ENABLED = True
        _startup.start_bot()
        _FakeThread.instances[0]._alive = False  # simulate a crashed WS loop
        assert _startup.start_bot() is True
        assert len(_FakeThread.instances) == 2


class TestStartBotFromSavedConfig:
    def test_saved_block_is_applied_then_started(self, monkeypatch,
                                                 _preserve_feishu_state):
        import lib.feishu.startup as startup
        st = _preserve_feishu_state
        monkeypatch.setattr('lib.json_store.read_json',
                            lambda *a, **k: {'feishu': {'app_id': 'cli_saved',
                                                        'app_secret': 'sec_saved'}})
        started = []
        monkeypatch.setattr(startup, 'start_bot', lambda: started.append(1) or True)
        assert startup.start_bot_from_saved_config() is True
        assert (st.APP_ID, st.ENABLED) == ('cli_saved', True)
        assert started == [1]

    def test_missing_block_does_not_clobber_env_creds(self, monkeypatch,
                                                      _preserve_feishu_state):
        import lib.feishu.startup as startup
        st = _preserve_feishu_state
        st.APP_ID = 'cli_orig'
        monkeypatch.setattr('lib.json_store.read_json', lambda *a, **k: {})
        started = []
        monkeypatch.setattr(startup, 'start_bot', lambda: started.append(1) or False)
        assert startup.start_bot_from_saved_config() is False
        assert st.APP_ID == 'cli_orig'
        assert started == [1]


class TestHotReloadFeishuRoute:
    def test_status_counts_bounded_resident_sessions(self, monkeypatch,
                                                     _preserve_feishu_state):
        from quart import Quart

        from lib.feishu.user_state import FeishuUserSessionStore
        from routes import config as config_routes

        store = FeishuUserSessionStore(capacity=4)
        store.set_mode('ou-a', 'chat')
        store.set_mode('ou-b', 'tool')
        monkeypatch.setattr(
            _preserve_feishu_state, 'feishu_user_sessions', store)
        monkeypatch.setattr(
            config_routes, '_feishu_is_connected', lambda: False)
        app = Quart(__name__)

        async def request_status():
            async with app.test_request_context('/api/v1/feishu/status'):
                response, status = config_routes.feishu_status()
                return status, await response.get_json()

        status, body = asyncio.run(request_status())

        assert status == 200
        assert body['ok'] is True
        assert body['active_users'] == 2

    def test_gui_save_applies_and_starts_bot(self, monkeypatch, _preserve_feishu_state):
        from routes.config import _hot_reload_feishu
        st = _preserve_feishu_state
        st.APP_ID, st.APP_SECRET, st.ENABLED = '', '', False
        started = []
        monkeypatch.setattr('lib.feishu.startup.is_bot_running', lambda: False)
        monkeypatch.setattr('lib.feishu.startup.start_bot',
                            lambda: started.append(1) or True)
        _hot_reload_feishu({'app_id': 'cli_gui', 'app_secret': 'sec_gui',
                            'allowed_users': ['ou_gui']})
        assert (st.APP_ID, st.ENABLED) == ('cli_gui', True)
        assert st.ALLOWED_USERS == {'ou_gui'}
        assert started == [1]

    def test_creds_change_while_running_does_not_restart(self, monkeypatch,
                                                         _preserve_feishu_state):
        from routes.config import _hot_reload_feishu
        st = _preserve_feishu_state
        st.APP_ID, st.APP_SECRET, st.ENABLED = 'cli_old', 'sec_old', True
        monkeypatch.setattr('lib.feishu.startup.is_bot_running', lambda: True)
        forbidden = mock.Mock()
        monkeypatch.setattr('lib.feishu.startup.start_bot', forbidden)
        _hot_reload_feishu({'app_id': 'cli_new', 'app_secret': 'sec_new'})
        assert st.APP_ID == 'cli_new'  # state applied for the next restart
        forbidden.assert_not_called()  # live WS keeps the old app until restart
