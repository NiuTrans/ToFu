"""Opening a conversation keeps its painted tail still.

Owner report (2026-11-02): clicking a conversation to read its final answer,
the page flickered once as if the content above shifted, and the final answer
sometimes disappeared and reappeared. Selection cleared the composer's model
notice, so the composer collapsed while the conversation loaded; the first
commit pinned the tail for that taller pane, then rendered the notice in the
same frame, so the first painted frame clipped the tail until a next-frame
correction moved the whole transcript. A notice taller than a short final
answer hid that answer for the frame.

Real local app and browser, synthetic owner-scoped history; every painted
frame of cold and warm opens is sampled. No provider calls.
"""
from __future__ import annotations

import uuid

import pytest

pytestmark = pytest.mark.visual

#: Samples what the reader just saw, after each painted frame.
_SAMPLER = r"""
(() => {
  const state = {frames: [], running: false};
  const sample = () => {
    const viewport = document.getElementById('chatContainer');
    const surface = document.querySelector('#chatInner [data-conversation-surface]');
    const blocks = surface ? surface.querySelectorAll('[data-turn-id] [data-block-id]') : [];
    const last = blocks[blocks.length - 1];
    const pane = viewport.getBoundingClientRect();
    const box = last ? last.getBoundingClientRect() : null;
    const notice = document.getElementById('composerModelNotice');
    state.frames.push({
      pane: viewport.clientHeight,
      conversation: surface?.dataset.conversationId || '',
      turns: surface ? surface.querySelectorAll('[data-turn-id]').length : 0,
      tailGap: Math.round(viewport.scrollHeight - viewport.scrollTop - viewport.clientHeight),
      scrollable: viewport.scrollHeight > viewport.clientHeight + 1,
      lastOnScreen: Boolean(box && box.bottom > pane.top + 1 && box.top < pane.bottom - 1),
      composer: Math.round(document.querySelector('.input-area').getBoundingClientRect().height),
      notice: notice?.checkVisibility({visibilityProperty: true}) ? notice.textContent : '',
    });
  };
  const loop = () => {
    if (!state.running) return;
    requestAnimationFrame(() => { setTimeout(sample, 0); loop(); });
  };
  window.__openStability = {
    start() { state.frames = []; state.running = true; sample(); loop(); },
    stop() { state.running = false; return state.frames; },
  };
})();
"""

#: Conversation -> (rounds, answer characters, served model or None).
_SHAPES = {
    'start': (2, 400, 'kimi-k3'),
    'many': (8, 900, 'glm-5'),
    'long': (1, 9000, 'gpt-6.1-sol'),
    'plain': (4, 1200, None),
}


def _seed(context, origin, cid, rounds, answer_chars, model):
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
        append(f'q{index}', 'human', {'content': f'Question {index} for {cid}'}, create=index == 0)
        answer = f'Answer {index} for {cid}. ' + 'Reading the final answer. ' * (answer_chars // 26)
        projection = {'content': answer, 'segments': [
            {'type': 'text', 'blockId': 'answer', 'text': answer, 'terminal': True, 'deliverable': True}]}
        if model:
            projection['lastRoundUsage'] = {'model': model, 'resolvedModel': model}
        append(f'a{index}', 'assistant', projection)


def test_opening_a_conversation_keeps_the_followed_tail_and_composer_still(browser, live_server):
    from playwright.sync_api import expect
    from tests.conftest import _dismiss_onboarding_modals

    prefix = 'open-still-' + uuid.uuid4().hex[:8]
    ids = {name: f'{prefix}-{name}' for name in _SHAPES}
    context = browser.new_context(viewport={'width': 1440, 'height': 900}, service_workers='block')
    context.add_init_script(f"sessionStorage.setItem('tofu_activeConvId', '{ids['start']}');")
    page = context.new_page()
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    try:
        for name, (rounds, chars, model) in _SHAPES.items():
            _seed(context, live_server, ids[name], rounds, chars, model)
        page.goto(live_server, wait_until='domcontentloaded')
        _dismiss_onboarding_modals(page)
        for cid in ids.values():
            expect(page.locator(f'.conv-item[data-conv-id="{cid}"]')).to_have_count(1, timeout=20000)
        expect(page.locator('#chatInner')).to_contain_text(f'Answer 1 for {ids["start"]}', timeout=20000)
        # The reported failure needs a served-model notice in the composer.
        expect(page.locator('#composerModelNotice')).to_contain_text('kimi-k3', timeout=20000)
        page.wait_for_timeout(500)
        _dismiss_onboarding_modals(page)
        page.evaluate(_SAMPLER)

        previous = 'start'
        for phase in ('cold', 'warm'):
            for name in ('many', 'long', 'plain'):
                cid, label = ids[name], f'{phase}-{name}'
                page.evaluate('window.__openStability.start()')
                page.locator(f'.conv-item[data-conv-id="{cid}"]').click(position={'x': 12, 'y': 12})
                expect(page.locator(f'#chatInner [data-conversation-surface][data-conversation-id="{cid}"]'
                                    ' [data-turn-id]').first).to_be_visible(timeout=20000)
                page.wait_for_timeout(1200)
                frames = page.evaluate('window.__openStability.stop()')
                first = next((index for index, frame in enumerate(frames)
                              if frame['conversation'] == cid and frame['turns']), None)
                assert first is not None, (label, frames[:3])
                painted = frames[first:]
                assert all(frame['conversation'] == cid for frame in painted), (label, painted[:4])
                assert painted[-1]['scrollable'], (f'{label}: the history must overflow the pane '
                                                   'for the tail to be pinned', painted[-1])
                clipped = [frame for frame in painted if frame['scrollable'] and frame['tailGap'] > 1]
                assert not clipped, (f'{label}: a painted frame left the followed tail clipped '
                                     'below the pane', clipped[:4])
                hidden = [frame for frame in painted if not frame['lastOnScreen']]
                assert not hidden, (f'{label}: the final answer left the screen after first paint',
                                    hidden[:4])
                expected = _SHAPES[name][2]
                assert (expected or '') in painted[-1]['notice'], (label, painted[-1])
                if _SHAPES[previous][2] and expected:
                    # Both conversations show a one-line notice: the composer
                    # keeps its height from the click to the settled frame.
                    resized = [frame for frame in frames
                               if abs(frame['composer'] - frames[0]['composer']) > 1]
                    assert not resized, (f'{label}: the composer changed height while the '
                                         'conversation opened', frames[0], resized[:4])
                previous = name

        # Chrome above the pane resizes it outside any commit, as the project
        # strip does when its reads land: plant the strip's return button.
        page.evaluate('window.__openStability.start()')
        page.evaluate("""() => {
          const button = document.createElement('button');
          button.className = 'ui-btn'; button.textContent = 'Return to Coordinator';
          document.getElementById('presenceStrip').append(button);
        }""")
        page.wait_for_timeout(600)
        frames = page.evaluate('window.__openStability.stop()')
        assert frames[-1]['pane'] < frames[0]['pane'] - 10, ('the strip did not shrink the pane',
                                                             frames[0], frames[-1])
        clipped = [frame for frame in frames if frame['scrollable'] and frame['tailGap'] > 1]
        assert not clipped, ('a strip mounted above the pane left the followed tail clipped',
                             clipped[:4])
        assert not errors, errors
    finally:
        for cid in ids.values():
            context.request.delete(f'{live_server}/api/v4/conversations/{cid}')
        context.close()
