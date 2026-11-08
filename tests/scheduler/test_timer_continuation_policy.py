"""A timer continuation runs as the agent that created the watcher.

Owner report (2026-11-02): a watcher created by a Claude Opus turn at max
reasoning depth delivered its continuation as a Kimi turn at the default depth
without the owner's prompt, and the continuation then rebuilt its whole model
context instead of reusing the creator's. ``build_task_config`` read the
conversation's stored ``model`` name, which disagreed with its routed
``modelRef`` (the settings resolver had merged the two from different
sources), and nothing carried the creating turn's depth, prompt or tool
exposure, so the model-replay policy witness never matched.

A watcher now captures the creating turn's policy and replays it as one
override. A row without that capture follows the conversation's own
selection, whose ``modelRef`` is the routing authority. Every scheduled turn
keeps the conversation's safety choices (Manual write approval, read-only
roots, Plan mode) and never gains an attended capability.
"""

from __future__ import annotations

import pytest

from lib.scheduler._shared import (
    CONTINUATION_POLICY_KEYS,
    build_task_config,
    capture_continuation_policy,
    continuation_policy,
)

pytestmark = pytest.mark.unit

OWNER = 7
OPUS_REF = {'creator_id': 'anthropic', 'model_id': 'claude-opus-5.5'}

# The stored settings of the reported conversation: the model name and the
# routed reference came from different sources.
STALE_SETTINGS = {
    'model': 'kimi-k3.1-pro', 'preset': 'kimi-k3.1-pro', 'modelRef': OPUS_REF,
    'thinkingDepth': 'max', 'searchMode': 'multi', 'codeExecEnabled': True,
}

CREATING_TURN_CONFIG = {
    'model': 'claude-opus-5.5', 'preset': 'claude-opus-5.5', 'modelRef': OPUS_REF,
    'preferredProviderId': 'sankuai',
    'routing': {'preferred_provider_id': 'sankuai'},
    'thinkingDepth': 'max', 'thinkingEnabled': True,
    'maxTokens': 64000, 'temperature': 0.2,
    'systemPrompt': 'Answer in Chinese.', 'systemPromptMode': 'append',
    'systemPromptBlocks': {'disabled': ['memory']},
    'chatMode': 'chat', 'delegateMode': False, 'schedulerEnabled': False,
    'mcpToolExposure': 'auto',
    'projectPaths': ['/proj'], 'readOnlyPaths': ['/proj/vendor'],
    'tools': {'toolSearch': 'auto'}, 'cache': {}, 'responses': {}, 'compaction': {},
    'searchMode': 'multi', 'fetchEnabled': True, 'codeExecEnabled': True,
    'memoryEnabled': True, 'projectPath': '/proj',
    # Attended capabilities and internal carriers are never captured.
    'humanGuidanceEnabled': True, 'desktopEnabled': True,
    '_principalContext': {'tenant_id': 't-1'},
}


# ── Conversation route and safety (every scheduled turn) ────────────────────

def test_conversation_route_follows_model_ref_over_a_stale_name():
    config = build_task_config({}, STALE_SETTINGS, owner=OWNER)
    assert config['model'] == config['preset'] == 'claude-opus-5.5'
    assert config['modelRef'] == OPUS_REF
    assert config['thinkingDepth'] == 'max'
    assert config['thinkingEnabled'] is True


def test_conversation_without_a_model_ref_keeps_its_model_name():
    config = build_task_config({}, {'model': 'kimi-k3'}, owner=OWNER)
    assert config['model'] == config['preset'] == 'kimi-k3'
    assert config['modelRef'] is None
    # No reasoning choice keeps the scheduler's historical thinking-on default.
    assert config['thinkingEnabled'] is True


def test_conversation_reasoning_off_is_respected():
    config = build_task_config({}, {'model': 'kimi-k3', 'thinkingDepth': 'off'}, owner=OWNER)
    assert config['thinkingEnabled'] is False


def test_scheduled_turn_keeps_the_conversations_safety_choices():
    config = build_task_config({'projectPath': '/proj'}, {
        'model': 'kimi-k3', 'autoApply': False,
        'projectPaths': ['/proj', '/shared'], 'readOnlyPaths': ['/shared'],
        'humanGuidanceEnabled': True, 'desktopEnabled': True,
        'autopilotEnabled': True,
    }, owner=OWNER)
    assert config['autoApply'] is False
    assert config['projectPaths'] == ['/proj', '/shared']
    assert config['readOnlyPaths'] == ['/shared']
    # Explicitly off: never granted, and recorded as a human turn records it.
    for attended in ('autopilot', 'autopilotEnabled', 'desktopEnabled',
                     'humanGuidanceEnabled'):
        assert config[attended] is False, attended


def test_scheduled_definition_never_selects_the_model(monkeypatch):
    """``schedule_create`` tools_config is model-authored: the agent runs on
    the target conversation's selection, never on a name the model wrote."""
    import lib
    monkeypatch.setattr(lib, 'LLM_MODEL', 'owner-default-model')
    assert build_task_config({'model': 'definition-model'}, STALE_SETTINGS, owner=OWNER)['model'] \
        == 'claude-opus-5.5'
    # Without a conversation choice the owner's default model runs, as from a fresh composer.
    config = build_task_config({'model': 'definition-model'}, {}, owner=OWNER)
    assert config['model'] == config['preset'] == 'owner-default-model'


def test_a_model_reference_alone_is_the_conversations_choice(monkeypatch):
    """The owner default never pairs its name with a conversation's own reference;
    task registration aligns the name to the reference's route."""
    import lib
    monkeypatch.setattr(lib, 'LLM_MODEL', 'owner-default-model')
    reference = {'provider_id': 'provider-a', 'offering_id': 'offering-a'}
    config = build_task_config({}, {'modelRef': reference}, owner=OWNER)
    assert config['modelRef'] == reference
    assert 'owner-default-model' not in (config['model'], config['preset'])


def test_schedule_create_schema_does_not_advertise_a_model_key():
    from lib.scheduler.tool_defs import SCHEDULER_TOOLS

    create = next(tool for tool in SCHEDULER_TOOLS
                  if tool['function']['name'] == 'schedule_create')
    described = create['function']['parameters']['properties']['tools_config']['description']
    assert 'model' not in described.split('Keys:', 1)[1].split('.', 1)[0]


# ── Captured policy override ────────────────────────────────────────────────

def _captured() -> dict:
    return capture_continuation_policy(CREATING_TURN_CONFIG)


def test_capture_takes_exactly_the_continuation_policy():
    assert set(_captured()) == set(CONTINUATION_POLICY_KEYS)
    for never in ('humanGuidanceEnabled', 'desktopEnabled', '_principalContext'):
        assert never not in _captured()


def test_caller_supplied_tool_schemas_are_never_captured():
    """A list-valued ``tools`` is an API caller's schema grant, not an owner
    preference block."""
    captured = capture_continuation_policy({
        **CREATING_TURN_CONFIG,
        'tools': [{'type': 'function', 'function': {'name': 'host_tool'}}],
    })
    assert 'tools' not in captured


def test_captured_policy_round_trips_as_one_override():
    assert continuation_policy(_captured()) == _captured()


def test_partial_identity_never_mixes_with_the_conversation_route():
    """A captured name alone must not inherit the conversation's modelRef:
    routing binds the reference, pairing one model's name with another's
    route."""
    assert continuation_policy({'model': 'gpt-x'}) == {
        'model': 'gpt-x', 'preset': 'gpt-x', 'modelRef': None,
        'preferredProviderId': '', 'routing': {}, 'thinkingDepth': None,
    }


def test_rows_without_a_capture_yield_no_override():
    assert continuation_policy({}) == {}
    assert continuation_policy({'searchMode': 'multi', 'projectPath': '/p'}) == {}


def test_timer_create_captures_the_creating_turn_policy(monkeypatch):
    from lib.scheduler.executor._timer import _execute_timer_create

    seen: dict = {}

    def _create_timer(**kwargs):
        seen.update(kwargs)
        return {
            'id': 'tmr_capture', 'check_command': '', 'condition_kind': 'code',
            'condition_command': 'test -f ready', 'poll_interval': 60,
            'max_polls': 120,
        }

    monkeypatch.setattr('lib.scheduler.timer.create_timer', _create_timer)
    monkeypatch.setattr('lib.scheduler.timer.start_timer_loop',
                        lambda *_a, **_k: None)

    _execute_timer_create({
        '_user_id': 7,
        '_source_conv_id': 'conv-1',
        'continuation_message': 'continue',
        'condition_command': 'test -f ready',
        '_parent_task': {'config': dict(CREATING_TURN_CONFIG)},
    })

    tools = seen['tools_config']
    for key in CONTINUATION_POLICY_KEYS:
        assert tools[key] == CREATING_TURN_CONFIG[key], key
    assert tools['projectPath'] == '/proj'
    for never in ('humanGuidanceEnabled', 'desktopEnabled', '_principalContext'):
        assert never not in tools


# ── Delivery: the config the continuation turn actually starts with ─────────

class _Commands:
    def __init__(self):
        self.calls: list[dict] = []

    def create_turn(self, conv_id, owner_id, payload, *, request_started_at):
        self.calls.append(payload)

        class _Outcome:
            value = {'attempt': {'taskId': 'task-1'}}

        return _Outcome()


class _ReceiptClient:
    def command(self, _operation, _payload, _command_id):
        return {'changed': True}


def _install_delivery(monkeypatch, tools_config: dict, settings: dict):
    import lib.scheduler.conversation_dispatch as dispatch_module
    from lib.scheduler.timer import _loop, _notify

    class _Storage:
        def query(self, operation, payload):
            assert operation == 'conversation.get'
            return {'metadata': {'settings': dict(settings)}}

    row = {
        'id': 'tmr_policy', 'status': 'active', 'user_id': 7,
        'conv_id': 'conv-1', 'continuation_message': 'continue',
        'tools_config': tools_config,
    }
    commands = _Commands()
    monkeypatch.setattr(dispatch_module, 'get_storage_client', lambda: _Storage())
    monkeypatch.setattr(dispatch_module, 'conversation_turn_commands', commands)
    monkeypatch.setattr(_loop, '_get_timer_row', lambda *a, **k: dict(row))
    monkeypatch.setattr(_loop, '_timer_client', lambda **kw: _ReceiptClient())
    monkeypatch.setattr(_loop, 'remove_live_watcher', lambda *_a: None)
    monkeypatch.setattr(_notify, 'notify_timer_changed', lambda *a, **k: None)
    return _loop, row, commands


def _deliver(monkeypatch, tools_config: dict, settings: dict = STALE_SETTINGS) -> dict:
    _loop, row, commands = _install_delivery(monkeypatch, tools_config, settings)
    assert _loop._execute_continuation(row).kind == 'started'
    assert len(commands.calls) == 1
    return commands.calls[0]


def _timer_tools_config(creator: dict) -> dict:
    """What ``timer_create`` stores for a watcher armed by ``creator``."""
    tools = {key: creator[key] for key in (
        'projectPath', 'searchMode', 'fetchEnabled', 'codeExecEnabled',
        'browserEnabled', 'browserClientId', 'imageGenEnabled',
        'memoryEnabled', 'autoTranslate', 'uiLang') if key in creator}
    tools.update(capture_continuation_policy(creator))
    return tools


def test_continuation_starts_with_the_creating_turns_policy(monkeypatch):
    payload = _deliver(monkeypatch, _timer_tools_config(CREATING_TURN_CONFIG))
    config = payload['config']
    for key in CONTINUATION_POLICY_KEYS:
        assert config[key] == CREATING_TURN_CONFIG[key], key
    assert config['projectPath'] == '/proj'
    assert config['humanGuidanceEnabled'] is False
    assert payload['inputTurn']['_ctx']['model'] == 'claude-opus-5.5'


def test_legacy_timer_follows_the_conversation_model_ref(monkeypatch):
    """Watchers created before the capture (still live after an upgrade)
    must not fall back to the disagreeing stored name either."""
    payload = _deliver(monkeypatch, {'searchMode': 'multi'})
    config = payload['config']
    assert config['model'] == 'claude-opus-5.5'
    assert config['modelRef'] == OPUS_REF
    assert config['thinkingDepth'] == 'max'
    assert payload['inputTurn']['_ctx']['model'] == 'claude-opus-5.5'


def test_chat_mode_that_would_grant_tools_is_dropped(monkeypatch):
    narrower = {**CREATING_TURN_CONFIG, 'codeExecEnabled': False}
    config = _deliver(monkeypatch, _timer_tools_config(narrower))['config']
    assert 'chatMode' not in config
    assert config['codeExecEnabled'] is False


def test_plan_mode_defers_delivery_without_accepting_anything(monkeypatch):
    _loop, row, commands = _install_delivery(
        monkeypatch, _timer_tools_config(CREATING_TURN_CONFIG),
        {**STALE_SETTINGS, 'planMode': True})
    assert _loop._execute_continuation(row).kind == 'retry'
    assert commands.calls == []


def test_continuation_shares_the_creating_turns_replay_witness(monkeypatch):
    """The continuation reuses the creator's replayed model context only when
    every policy witness field matches; any drift forces a cold rebuild."""
    from lib.conv_config import resolve_conv_config
    from lib.tasks_pkg.model_replay import _policy_fields, policy_witness_drift
    from lib.tasks_pkg.plan_mode import normalize_interaction_mode_runtime_config

    settings = {
        'model': 'claude-opus-5.5', 'modelRef': OPUS_REF, 'thinkingDepth': 'max',
        'chatMode': 'chat', 'searchMode': 'multi', 'fetchEnabled': True,
        'codeExecEnabled': True, 'memoryEnabled': True, 'schedulerEnabled': False,
        'humanGuidanceEnabled': False, 'desktopEnabled': False,
        'autopilotEnabled': False, 'projectPath': '/proj', 'autoTranslate': True,
        'uiLang': 'zh',
    }
    # The browser's toolbar overrides for a send in this conversation.
    overrides = {
        **settings, 'preferredProviderId': '', 'routing': {},
        'systemPrompt': 'Answer in Chinese.', 'systemPromptMode': 'append',
        'systemPromptBlocks': {}, 'mcpToolExposure': 'auto', 'tools': {},
    }
    creator = normalize_interaction_mode_runtime_config(
        resolve_conv_config(settings, overrides, is_active=True))
    continuation = normalize_interaction_mode_runtime_config(
        _deliver(monkeypatch, _timer_tools_config(creator), settings)['config'])

    checkpoint = {'policyFields': _policy_fields(creator)}
    assert policy_witness_drift(checkpoint, continuation) == []


# ── A steered continuation that fell back to the queue ──────────────────────

class _RecordingClient:
    def __init__(self, result=None, error=None):
        self.commands: list[tuple] = []
        self._result = result if result is not None else {'changed': True}
        self._error = error

    def command(self, operation, payload, command_id):
        self.commands.append((operation, payload, command_id))
        if self._error is not None:
            raise self._error
        return self._result


def test_requeued_steer_corrects_the_timer_receipt(monkeypatch):
    from lib.scheduler.timer import _loop, _notify

    client = _RecordingClient()
    notes: list[str] = []
    monkeypatch.setattr(_loop, '_timer_client', lambda **kw: client)
    monkeypatch.setattr(_notify, 'notify_timer_changed',
                        lambda change, **kw: notes.append(change))

    assert _loop.record_requeued_continuation('timer:tmr_q', user_id=7) is True
    operation, payload, _command_id = client.commands[0]
    assert operation == 'timer.update'
    assert payload['timer_id'] == 'tmr_q' and payload['user_id'] == 7
    assert payload['last_poll_decision'] == 'queued'
    # Only the settled watcher's receipt moves; an active row is untouched.
    assert payload['expected_status'] == 'triggered'
    assert notes == ['progress']


def test_other_steers_leave_timers_untouched(monkeypatch):
    from lib.scheduler.timer import _loop

    client = _RecordingClient()
    monkeypatch.setattr(_loop, '_timer_client', lambda **kw: client)
    assert _loop.record_requeued_continuation('ui-command-1', user_id=7) is False
    assert client.commands == []


def test_receipt_failure_never_breaks_the_salvage(monkeypatch):
    from lib.scheduler.timer import _loop, _notify

    notes: list[str] = []
    monkeypatch.setattr(_loop, '_timer_client',
                        lambda **kw: _RecordingClient(error=RuntimeError('down')))
    monkeypatch.setattr(_notify, 'notify_timer_changed',
                        lambda change, **kw: notes.append(change))
    assert _loop.record_requeued_continuation('timer:tmr_q', user_id=7) is False
    assert notes == []
