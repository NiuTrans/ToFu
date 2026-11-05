"""Opening a conversation never moves the reading column or the composer.

Owner report (2026-11-01): clicking a sidebar conversation painted the turns
centered, then the context rail appeared and pushed them left. The Surface
used to collapse the shell's context track until a mounted Turn showed a rail,
so the loading frame, and a byte-budgeted first page holding only the round's
huge reply, painted the no-rail geometry first. The track is now reserved
until the loaded transcript proves a conversation rail-less.

Real local app and browser, synthetic owner-scoped history; every painted
frame of each switch is sampled. No provider calls.
"""
from __future__ import annotations

import uuid

import pytest

pytestmark = pytest.mark.visual

#: Samples the geometry the reader just saw, after each painted frame.
_SAMPLER = r"""
(() => {
  const state = {frames: [], running: false};
  const left = (selector) => {
    const node = document.querySelector(selector);
    return node ? Math.round(node.getBoundingClientRect().left * 10) / 10 : null;
  };
  const sample = () => {
    const surface = document.querySelector('#chatInner .conversation-surface');
    const rails = [...document.querySelectorAll('#chatInner .turn-ctx:not([hidden])')]
      .filter((node) => node.getBoundingClientRect().width > 0);
    state.frames.push({
      conversation: surface?.dataset.conversationId || '',
      latch: surface?.dataset.turnContextRail || '',
      turns: surface ? surface.querySelectorAll('[data-turn-id]').length : 0,
      column: left('#chatInner'),
      composer: left('.input-inner'),
      rails: rails.length,
    });
  };
  const loop = () => {
    if (!state.running) return;
    requestAnimationFrame(() => { setTimeout(sample, 0); loop(); });
  };
  window.__railSwitchProbe = {
    start() { state.frames = []; state.running = true; loop(); },
    stop() { state.running = false; return state.frames; },
  };
})();
"""

_TOOLS = [{'label': label, 'tone': tone} for label, tone in (
    ('Search ×N', 'search'), ('Fetch', 'net'), ('Code Exec', 'code'), ('Memory', 'ai'),
    ('Browser', 'net'), ('MCP: overleaf ×1', 'mcp'))]


def _seed(context, origin, cid, rounds, huge_last_reply=False):
    def append(command, actor, projection, create=False):
        body = {'commandId': f'{cid}-{command}', 'actor': actor, 'kind': 'text',
                'projection': projection}
        if create:
            body['conversation'] = {'allowCreate': True, 'title': cid, 'createdAt': 100,
                                    'settings': {}}
        response = context.request.post(
            f'{origin}/api/v4/conversations/{cid}/turns/settled', data=body)
        assert response.status == 200, response.text()

    for index in range(rounds):
        snapshot = {'blockId': f'ctx-{index}', 'snapshot': {
            'model': 'claude-opus-4-5', 'depth': 'max', 'modes': [], 'tools': _TOOLS,
            'roots': [{'short': 'team/project', 'path': '/w/team/project', 'ro': False}]}}
        append(f'q{index}', 'human', {'content': f'Question {index}', 'contextSnapshot': snapshot},
               create=index == 0)
        segments, rounds_ = [], []
        if huge_last_reply and index == rounds - 1:
            # Large enough that the byte-budgeted first page holds only this reply.
            for step in range(120):
                identity = {'llmRound': step, 'attemptId': 'attempt', 'taskId': 'task'}
                record = {**identity, 'roundNum': step + 1, 'toolCallId': f'call-{step}',
                          'toolName': 'read_files', 'status': 'done', 'toolArgs': {'path': 'x.txt'},
                          'toolContent': 'Synthetic historical result ' * 200}
                rounds_.append(record)
                segments += [
                    {'type': 'thinking', 'blockId': f'thinking-{step}', 'text': 'thought ' * 50, **identity},
                    {'type': 'tool_use', 'blockId': f'tool-{step}', 'id': f'call-{step}', 'name': 'read_files',
                     'input': record['toolArgs'],
                     'result': {'status': 'done', 'content': record['toolContent']}, **identity},
                ]
        answer = f'Answer {index} for {cid}. ' + 'Reading column text. ' * 40
        segments.append({'type': 'text', 'blockId': 'answer', 'text': answer,
                         'terminal': True, 'deliverable': True})
        projection = {'content': answer, 'segments': segments}
        if rounds_:
            projection['toolRounds'] = rounds_
        append(f'a{index}', 'assistant', projection)


def test_switching_conversations_keeps_the_two_zone_geometry_still(browser, live_server):
    from playwright.sync_api import expect
    from tests.conftest import _dismiss_onboarding_modals

    prefix = 'rail-switch-' + uuid.uuid4().hex[:8]
    huge, small, start = prefix + '-huge', prefix + '-small', prefix + '-start'
    context = browser.new_context(viewport={'width': 1680, 'height': 1000}, service_workers='block')
    context.add_init_script(f"sessionStorage.setItem('tofu_activeConvId', '{start}');")
    page = context.new_page()
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    try:
        _seed(context, live_server, start, 1)
        _seed(context, live_server, small, 3)
        _seed(context, live_server, huge, 3, huge_last_reply=True)
        page.goto(live_server, wait_until='domcontentloaded')
        _dismiss_onboarding_modals(page)
        for cid in (huge, small):
            expect(page.locator(f'.conv-item[data-conv-id="{cid}"]')).to_have_count(1, timeout=20000)
        expect(page.locator('#chatInner .turn-ctx:not([hidden])').first).to_be_visible(timeout=20000)
        page.wait_for_timeout(500)
        _dismiss_onboarding_modals(page)
        page.evaluate(_SAMPLER)
        settled = page.evaluate("""() => ({column: document.querySelector('#chatInner').getBoundingClientRect().left,
          composer: document.querySelector('.input-inner').getBoundingClientRect().left})""")

        for label, cid in (('cold-huge', huge), ('cold-small', small),
                           ('warm-huge', huge), ('warm-small', small)):
            page.evaluate('window.__railSwitchProbe.start()')
            page.locator(f'.conv-item[data-conv-id="{cid}"]').click(position={'x': 12, 'y': 12})
            expect(page.locator(f'#chatInner .conversation-surface[data-conversation-id="{cid}"]'
                                ' .turn-ctx:not([hidden])').first).to_be_visible(timeout=20000)
            page.wait_for_timeout(400)
            frames = page.evaluate('window.__railSwitchProbe.stop()')
            opened = [frame for frame in frames if frame['conversation'] == cid]
            assert opened, (label, frames[:3])
            moved = [frame for frame in frames
                     if abs(frame['column'] - settled['column']) > 1
                     or abs(frame['composer'] - settled['composer']) > 1]
            assert not moved, (f'{label}: the reading column or composer moved while the '
                               f'conversation opened', moved[:4], settled)
            assert all(frame['latch'] != 'absent' for frame in opened), (label, opened[:4])
            assert opened[-1]['latch'] == 'present' and opened[-1]['rails'] >= 1, (label, opened[-1])
            if label == 'cold-huge':
                # The reported failure mode was exercised: content painted
                # before the round's input Turn (and its rail) had landed.
                assert any(frame['turns'] and not frame['rails'] for frame in opened), (
                    'the first page already held the round opening; this spec no '
                    'longer covers the reply-only page', opened[:4])
        assert not errors, errors
    finally:
        for cid in (huge, small, start):
            context.request.delete(f'{live_server}/api/v4/conversations/{cid}')
        context.close()
