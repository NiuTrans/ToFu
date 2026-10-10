"""Real shipped footer formats server evidence across reload and browser clock changes."""
import time
from uuid import uuid4

import pytest

pytestmark = [pytest.mark.visual, pytest.mark.auth_mode('open')]


@pytest.mark.parametrize('width', [1280, 390])
@pytest.mark.parametrize('evidence', ['full', 'partial', 'missing'])
def test_finish_timing_is_server_owned(page, monkeypatch, tmp_path, width, evidence):
    from playwright.sync_api import expect
    from lib.storage import get_storage_client
    from lib.tasks_pkg import turn_trace
    from lib.turn_lifecycle import append_settled_turn
    from tests.conftest import _dismiss_onboarding_modals

    conv_id = 'visual-server-clock-' + uuid4().hex[:12]
    task_id = conv_id + '-task'
    origin = int(time.time() * 1000) - 42000
    boundaries = [
        ('round_start', 0, {'roundNum': 1}),
        ('tool_start', 10000, {'roundNum': 1, 'toolName': 'read_files',
                             'toolCallId': 'call-a', 'toolStartTs': origin + 10000}),
        ('tool_result', 15000, {'roundNum': 1, 'toolCallId': 'call-a',
                              'toolEndTs': origin + 15000}),
        ('round_end', 20000, {'roundNum': 1}),
        ('round_start', 23000, {'roundNum': 2}),
        ('round_end', 41000, {'roundNum': 2}),
        ('done', 42000, {}),
    ]
    rows = [
        {'event_id': index, 'type': kind, 'ts_ms': origin + offset,
         'payload': {'type': kind, **payload, 'executionAtMs': origin + offset,
                     'executionOriginAtMs': origin, 'executionClockId': 'visual-clock'}}
        for index, (kind, offset, payload) in enumerate(boundaries)
    ]
    with monkeypatch.context() as controlled_events:
        controlled_events.setattr(turn_trace, '_read_trace_rows', lambda _: rows)
        trace = turn_trace.fold_task_trace(task_id, now_ms=origin + 999999)
    assert trace['clockSource'] == 'server-monotonic'
    assert trace['coverage'] == 'full' and trace['totalMs'] == 42000
    projection = {'content': 'Server clock browser evidence'}
    if evidence != 'missing':
        if evidence == 'partial':
            trace['coverage'] = 'partial'
            trace['coverageReason'] = 'event-limit'
        projection['timingTrace'] = trace
    client = get_storage_client(write=True)
    try:
        result = append_settled_turn(
            conv_id, command_id=conv_id, actor='assistant', user_id=1,
            run_id=task_id, projection=projection, created_at=1,
            conversation_defaults={'allowCreate': True, 'title': 'Server clock evidence',
                                   'createdAt': 1, 'settings': {}})
        page.set_viewport_size({'width': width, 'height': 900})
        page.reload(wait_until='domcontentloaded')
        _dismiss_onboarding_modals(page)
        page.locator(f'.conv-item[data-conv-id="{conv_id}"]').wait_for()
        page.evaluate('Date.now = () => 1000')
        page.wait_for_function(
            "typeof window.TofuModules?.resolveAction?.('loadConversation') === 'function'")
        page.evaluate("id => window.TofuModules.resolveAction('loadConversation')(id)", conv_id)
        turn = page.locator(f'[data-turn-id="{result["turn"]["turnId"]}"]')
        expect(turn).to_contain_text('Server clock browser evidence')
        timing = turn.locator('.finish-tag.timing')
        expect(timing).to_be_visible()
        text = timing.inner_text()
        if evidence == 'full':
            assert text.endswith('· 42s'), text
        else:
            assert '·' not in text, text
        page.reload(wait_until='domcontentloaded')
        _dismiss_onboarding_modals(page)
        expect(timing).to_be_visible()
        assert timing.inner_text() == text
        screenshot = tmp_path / f'server-clock-{evidence}-{width}.png'
        turn.screenshot(path=str(screenshot))
        print(f'Server execution clock screenshot: {screenshot}')
    finally:
        client.command('conversation.delete', {'conv_id': conv_id, 'user_id': 1},
                       f'{conv_id}:cleanup')
