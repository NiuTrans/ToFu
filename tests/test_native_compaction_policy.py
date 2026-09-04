"""Provider-native compaction routing and local-L2 fallback contracts."""

from types import SimpleNamespace

import pytest

from lib.llm._sse_core import prepare_request
from lib.llm_dispatch.compaction_policy import (
    ANTHROPIC_MESSAGES_COMPACTION,
    OPENAI_RESPONSES_COMPACTION,
    native_compaction_mode_for_route,
    resolve_task_native_compaction_mode,
)
from lib.tasks_pkg.manager._stream import _opaque_reasoning_replay_tokens

pytestmark = pytest.mark.unit


def _slot(**overrides):
    values = {
        'provider_id': 'public',
        'logical_model': 'gpt-5.6-sol',
        'model': 'gpt-5.6-sol',
        'protocol': 'responses',
        'responses_profile': 'openai',
        'base_url': 'https://api.openai.com/v1',
        'oauth': '',
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_exact_route_separates_public_apis_from_subscription_oauth():
    assert native_compaction_mode_for_route(
        protocol='responses', model='gpt-5.6-sol',
        responses_profile='openai',
        base_url='https://api.openai.com/v1') == (
            OPENAI_RESPONSES_COMPACTION)
    assert native_compaction_mode_for_route(
        protocol='responses', model='gpt-5.6-sol',
        responses_profile='openai',
        base_url='https://chatgpt.com/backend-api/codex',
        oauth='codex') == ''

    assert native_compaction_mode_for_route(
        protocol='anthropic', model='claude-opus-5',
        base_url='https://api.anthropic.com/v1') == (
            ANTHROPIC_MESSAGES_COMPACTION)
    assert native_compaction_mode_for_route(
        protocol='anthropic', model='claude-opus-5',
        base_url='https://api.anthropic.com/v1', oauth='claude') == ''
    assert native_compaction_mode_for_route(
        protocol='anthropic', model='claude-opus-4-5-20251101',
        base_url='https://api.anthropic.com/v1') == ''


def test_pre_dispatch_policy_requires_unanimous_candidate_slots(monkeypatch):
    import lib.llm_dispatch.factory as factory

    public = _slot()
    subscription = _slot(
        provider_id='oauth_codex', responses_profile='', oauth='codex',
        base_url='https://chatgpt.com/backend-api/codex')
    monkeypatch.setattr(
        factory, 'get_dispatcher',
        lambda: SimpleNamespace(slots=[public, subscription]))

    assert resolve_task_native_compaction_mode(
        {'config': {}}, model='gpt-5.6-sol') == ''
    assert resolve_task_native_compaction_mode(
        {'provider_id': 'public', 'config': {}},
        model='gpt-5.6-sol') == OPENAI_RESPONSES_COMPACTION
    assert resolve_task_native_compaction_mode(
        {'provider_id': 'oauth_codex', 'config': {}},
        model='gpt-5.6-sol') == ''


def test_public_anthropic_plan_emits_compaction_beta_and_wire_strategy():
    plan = prepare_request(
        {
            'model': 'claude-opus-5',
            'max_tokens': 256,
            'stream': True,
            '_working_set_tokens': 128_000,
            'messages': [{'role': 'user', 'content': 'continue'}],
        },
        api_key='test-key',
        base_url='https://api.anthropic.com/v1',
        api_protocol='anthropic',
    )

    assert plan.native_compaction_mode == ANTHROPIC_MESSAGES_COMPACTION
    assert 'compact-2026-01-12' in plan.hdrs['anthropic-beta'].split(',')
    edit = plan.body['context_management']['edits'][0]
    assert edit['type'] == 'compact_20260112'
    assert edit['trigger'] == {'type': 'input_tokens', 'value': 128_000}
    assert 'Do not call tools' in edit['instructions']


def test_native_primary_defers_economic_l2_but_keeps_hard_window(monkeypatch):
    import lib.tasks_pkg.compaction._tokens as tokens

    task = {
        'convId': 'native-primary-gate',
        'config': {'model': 'gpt-5.6-sol'},
        '_nativeCompactionPrimary': True,
        '_nativeCompactionMode': OPENAI_RESPONSES_COMPACTION,
    }
    monkeypatch.setattr(
        tokens, '_compaction_trigger_threshold',
        lambda *_a, **_k: (128_000, 900_000, 128_000))
    measured = {'value': 200_000}
    monkeypatch.setattr(
        tokens, '_count_tokens_authoritative',
        lambda *_a, **_k: (measured['value'], 'usage_cache'))

    messages = [{'role': 'user', 'content': 'x'}]
    assert tokens._should_force_compact(messages, task) is False
    measured['value'] = 900_001
    assert tokens._should_force_compact(messages, task) is True


def test_provider_reasoning_usage_becomes_one_round_replay_reserve():
    usage = {'thinking': 9_000}
    assert _opaque_reasoning_replay_tokens({
        '_responses_items': [{
            'type': 'reasoning', 'encrypted_content': 'opaque'}],
    }, usage) == 9_000
    assert _opaque_reasoning_replay_tokens({
        '_anthropic_content_blocks': [{
            'type': 'redacted_thinking', 'data': 'opaque'}],
    }, usage) == 9_000
    assert _opaque_reasoning_replay_tokens({
        'reasoning_content': 'visible thinking only',
    }, usage) == 0
