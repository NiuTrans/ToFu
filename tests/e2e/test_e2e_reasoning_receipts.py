"""Missing-summary receipts survive the real app, durable projection and reload.

The existing module-owned synthetic model stub is the only token source; the
normal task loop, usage projection, v4 sync, Vite delivery and Chromium run.
"""
from __future__ import annotations

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

    # Delegate to the already-owned offline stub so its real task DELTAs and
    # sentinel guard remain in force. Only settled provider metering differs.
    original = llm_call.stream_llm_response
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

    monkeypatch.setattr(llm_call, 'stream_llm_response', stream)
    monkeypatch.setattr(provider_stream, 'dispatch_stream', reject_provider)
    return calls


@pytest.mark.parametrize('width', [1280, 390])
def test_missing_summary_receipt_reaches_live_and_reloaded_app(
        reasoning_metered_reply, page, live_server, width, tmp_path, assert_no_js_errors):
    page.set_viewport_size({'width': width, 'height': 900})
    _fresh_chat(page)
    _send_and_wait_done(page, 'Offline reasoning receipt without a readable summary')
    assert len(reasoning_metered_reply) == 1
    status = page.locator('#chatInner .conversation-reasoning-status').last
    status.wait_for(state='visible', timeout=15000)
    assert status.get_attribute('data-reasoning-summary') == 'unavailable'
    assert '2,843' in status.locator('.conversation-reasoning-tokens').inner_text()
    assert status.locator('.conversation-reasoning-effort').inner_text().endswith(('Low', '低'))
    assert status.locator('details, button, a, img').count() == 0
    assert page.locator('#chatInner .thinking-block').count() == 0
    assert status.evaluate('node => !node.closest("details")')
    before = status.inner_text()

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
    restored = page.locator('#chatInner .conversation-reasoning-status').last
    restored.wait_for(state='visible', timeout=15000)
    assert page.locator('.conv-item.active').get_attribute('data-conv-id') == conversation_id
    assert restored.inner_text() == before
    assert restored.get_attribute('data-reasoning-summary') == 'unavailable'
    assert page.locator('#chatInner .thinking-block').count() == 0
    assert len(reasoning_metered_reply) == 1, 'reload must not execute another model request'
    geometry = restored.evaluate('''node => {
        const rect = node.getBoundingClientRect();
        return {left: rect.left, right: rect.right, width: innerWidth};
    }''')
    assert geometry['left'] >= 0 and geometry['right'] <= geometry['width'] + 1
    page.screenshot(path=str(tmp_path / f'reasoning-receipt-app-{width}.png'))
