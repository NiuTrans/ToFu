"""Reasoning evidence survives the real app and reload without header notices.

The existing module-owned synthetic model stub is the only token source; the
normal task loop, usage projection, v4 sync, Vite delivery and Chromium run.
"""
from __future__ import annotations

import lib.tasks_pkg.llm_fallback._transport as _fallback_transport

import pytest

from tests.e2e.test_e2e_journeys import _fresh_chat, _send_and_wait_done
from tests.e2e.test_e2e_smoke import (
    _disable_open_mode_rate_limit as _disable_open_mode_rate_limit,
    _install_llm_stubs as _install_llm_stubs,
    _wait_app_ready,
)

pytestmark = pytest.mark.visual


@pytest.fixture
def reasoning_metered_reply(_install_llm_stubs, monkeypatch):
    import lib.tasks_pkg.llm_fallback._call as llm_call
    import lib.tasks_pkg.manager._stream as provider_stream

    # Keep the real task DELTAs and sentinel guard of the existing offline stub.
    # Only settled provider metering differs; provider I/O remains forbidden.
    original = _fallback_transport.stream_llm_response
    calls = []

    def stream(task, body, **kwargs):
        message, finish, usage = original(task, body, **kwargs)
        calls.append(task['id'])
        assert not message.get('reasoning_content'), 'fixture must have no readable summary'
        return message, finish, {
            **usage,
            'completion_tokens': 2900,
            'total_tokens': int(usage['prompt_tokens']) + 2900,
            'completion_tokens_details': {'reasoning_tokens': 2843},
            '_reasoning_resolution': {
                'requested': 'preset', 'requestedValue': 'high',
                'effective': 'preset', 'actualMode': 'exact', 'actualValue': 'low',
                'source': 'declared',
            },
        }

    def reject_provider(*args, **kwargs):
        pytest.fail('reasoning receipt browser test attempted real provider I/O')

    monkeypatch.setattr(_fallback_transport, 'stream_llm_response', stream)
    monkeypatch.setattr(provider_stream, 'dispatch_stream', reject_provider)
    return calls


@pytest.mark.parametrize('width', [1280, 390])
def test_reasoning_evidence_survives_reload_without_header_notices(
        reasoning_metered_reply, page, live_server, width, tmp_path, assert_no_js_errors):
    page.set_viewport_size({'width': width, 'height': 900})
    _fresh_chat(page)
    _send_and_wait_done(page, 'Offline reasoning receipt without a readable summary')
    assert len(reasoning_metered_reply) == 1
    assert page.locator('#chatInner .conversation-reasoning-status, #chatInner [data-reasoning-summary]').count() == 0
    assert page.locator('#chatInner .thinking-block').count() == 0
    answer = page.locator('#chatInner [data-turn-role="assistant"] .md-content').last
    answer.wait_for(state='visible', timeout=15000)
    before = answer.inner_text()
    assert before

    conversation_id = page.locator('.conv-item.active').get_attribute('data-conv-id')
    assert conversation_id
    endpoint = f'{live_server}/api/v4/conversations/{conversation_id}/sync'
    snapshot = page.request.get(endpoint)
    assert snapshot.ok
    replies = [turn for turn in snapshot.json()['turns'] if turn.get('actor') == 'assistant']
    assert replies
    turn = replies[-1]
    assert turn['status'] == 'completed'
    projection = turn['projection']
    assert projection['lastRoundUsage']['reasoningResolution']['actualValue'] == 'low'
    assert projection['usage']['completion_tokens_details']['reasoning_tokens'] == 2843
    assert not projection.get('thinking')

    page.reload(wait_until='domcontentloaded')
    _wait_app_ready(page)
    restored = page.locator('#chatInner [data-turn-role="assistant"] .md-content').last
    restored.wait_for(state='visible', timeout=15000)
    assert page.locator('.conv-item.active').get_attribute('data-conv-id') == conversation_id
    assert restored.inner_text() == before
    assert page.locator('#chatInner .conversation-reasoning-status, #chatInner [data-reasoning-summary]').count() == 0
    assert page.locator('#chatInner .thinking-block').count() == 0
    assert len(reasoning_metered_reply) == 1, 'reload must not execute another model request'
    reloaded_snapshot = page.request.get(endpoint)
    assert reloaded_snapshot.ok
    reloaded_turn = next(row for row in reloaded_snapshot.json()['turns'] if row['turnId'] == turn['turnId'])
    assert reloaded_turn['projection']['usage'] == projection['usage']
    assert reloaded_turn['projection']['lastRoundUsage'] == projection['lastRoundUsage']
    geometry = restored.evaluate('''node => {
        const rect = node.getBoundingClientRect();
        return {left: rect.left, right: rect.right, width: innerWidth};
    }''')
    assert geometry['left'] >= 0 and geometry['right'] <= geometry['width'] + 1
    page.screenshot(path=str(tmp_path / f'no-reasoning-notice-app-{width}.png'))
