"""Scroll-driven history with real layout and shipped styles, no durable writes.

The static local-server fixture loads production Surface/selector modules. This
spec covers geometry and DOM retirement; transport/retry belongs to live-app
journeys and coordinator tests, not a substitute transport in this fixture.
"""
import json
import uuid

import pytest

from tests._runtime_sections import ROOT, native_module_path
from tests.support.frontend_visual import (  # noqa: F401
    frontend_visual_origin,
    frontend_visual_page as frontend_visual_page,
)

pytestmark = pytest.mark.visual


@pytest.mark.parametrize('width', [1180, 390])
@pytest.mark.parametrize('history_kind', ['blocks', 'turns'])
def test_scroll_history_preserves_reading_anchor_and_bounds_dom(
        frontend_visual_page, tmp_path, width, history_kind):
    page = frontend_visual_page
    page.set_viewport_size({'width': width, 'height': 800})
    page.evaluate("""() => {
      document.body.innerHTML='<main id="viewport"><div id="surface"></div></main>';
      document.body.style.cssText='display:block;margin:0;padding:0';
      document.querySelector('#viewport').style.cssText=
        'height:700px;overflow:auto;overflow-anchor:none;scroll-behavior:auto';
    }""")
    for name, source in [('feature', 'frontend/src/conversation/index.ts'),
                         ('state', 'frontend/src/core/turn-state.ts')]:
        page.add_script_tag(path=native_module_path(
            f'.native/scroll-history-{name}.js', ROOT / source))
    page.evaluate(r"""kind => {
      const cid='synthetic-scroll-'+kind;
      const makeTurn=i=>({turnId:'turn-'+i,conversationId:cid,laneId:'main',
        ordinal:i,actor:'assistant',kind:'reply',status:kind==='blocks'?'running':'completed',
        projectionRevision:1,currentAttemptId:null,createdAt:i,updatedAt:i,
        settlement:kind==='blocks'?{}:{outcome:'completed'},projection:{segments:
          Array.from({length:kind==='blocks'?450:1},(_,j)=>({type:'text',
            blockId:`turn-${i}:block-${j}`,
            text:`History ${i}/${j}\n\n`+'Readable synthetic evidence. '.repeat(12),
            terminal:kind==='blocks'?j===449:true,
            deliverable:kind==='blocks'?j===449:true}))}});
      const turns=Array.from({length:kind==='blocks'?1:300},(_,i)=>makeTurn(i));
      window.fixtureState={...createTurnState(cid),
        turnsById:Object.fromEntries(turns.map(t=>[t.turnId,t])),
        laneOrder:{main:turns.map(t=>t.turnId)}};
      window.surface=createConversationSurface(document.querySelector('#surface'),{
        scrollViewport:document.querySelector('#viewport'),
        windowing:{maxTurns:20,batchSize:5,maxBlocksPerTurn:60,blockBatchSize:20}});
      surface.render(selectConversationViewModel(fixtureState));
      window.readingSnapshot=()=>{
        const viewport=document.querySelector('#viewport');
        const rect=viewport.getBoundingClientRect();
        const blocks=[...document.querySelectorAll('[data-block-id]')];
        const firstVisible=blocks.find(n=>{
          const r=n.getBoundingClientRect();return r.bottom>rect.top && r.top<rect.bottom;
        });
        return {first:blocks[0]?.dataset.blockId,anchor:firstVisible?.dataset.blockId,
          y:firstVisible?.getBoundingClientRect().top-rect.top,
          blocks:blocks.length,turns:document.querySelectorAll('[data-turn-id]').length,
          overflow:document.documentElement.scrollWidth>innerWidth+1};
      };
      window.prepareScroll=()=>{
        const viewport=document.querySelector('#viewport');
        viewport.scrollTop=0;
        const snapshot=readingSnapshot();
        window.anchorNode=[...document.querySelectorAll('[data-block-id]')]
          .find(n=>n.dataset.blockId===snapshot.anchor);
        return snapshot;
      };
    }""", history_kind)
    page.wait_for_function("""() => {
      const v=document.querySelector('#viewport');
      return v.scrollHeight-v.scrollTop-v.clientHeight<=2;
    }""")
    assert page.locator('.conversation-round-toolbar:visible').count() == 0
    # An accessible focus-only keyboard fallback is allowed, but it must not
    # occupy visible reading space as an always-on history pill.
    assert page.evaluate("""() => [...document.querySelectorAll(
      '[data-conversation-window-action],[data-conversation-block-window-action]')]
      .every(n=>{const r=n.getBoundingClientRect(),s=getComputedStyle(n);
        return r.width<=2 || r.height<=2 || s.opacity==='0' || s.visibility==='hidden'
          || s.clip!=='auto' || s.clipPath!=='none';})""")
    measurements = []
    # Four batches demonstrate that repeated reading cannot grow the DOM.
    for _ in range(4):
        before = page.evaluate('prepareScroll()')
        assert before['anchor'], before
        page.wait_for_function(
            'first => readingSnapshot().first !== first', arg=before['first'])
        after = page.evaluate("""before => {
          const node=[...document.querySelectorAll('[data-block-id]')]
            .find(n=>n.dataset.blockId===before.anchor);
          const rect=document.querySelector('#viewport').getBoundingClientRect();
          return {...readingSnapshot(),sameNode:node===anchorNode,
            anchorY:node?.getBoundingClientRect().top-rect.top};
        }""", before)
        assert after['sameNode'], (before, after)
        assert abs(after['anchorY'] - before['y']) <= 2, (before, after)
        assert 0 < after['turns'] <= 20, after
        assert 0 < after['blocks'] <= 60, after
        assert not after['overflow'], after
        measurements.append({'before': before, 'after': after})
    # Reverse scrolling restores newer mounted content without a history pill.
    before_reverse = page.evaluate('readingSnapshot()')
    page.evaluate("""() => {const v=document.querySelector('#viewport');
      v.scrollTop=v.scrollHeight;}""")
    page.wait_for_function(
        'first => readingSnapshot().first !== first', arg=before_reverse['first'])
    reverse = page.evaluate('readingSnapshot()')
    assert reverse['turns'] <= 20 and reverse['blocks'] <= 60, reverse
    surface_tail = 'turn-0:block-449' if history_kind == 'blocks' else 'turn-299:block-0'
    page.evaluate('surface.followLatest()')
    page.wait_for_function("""last => {
      const blocks=document.querySelectorAll('[data-block-kind="text"]');
      const v=document.querySelector('#viewport');
      return blocks[blocks.length-1]?.dataset.blockId===last
        && v.scrollHeight-v.scrollTop-v.clientHeight<=2;
    }""", arg=surface_tail)
    assert page.locator('[data-block-kind="text"]').last.get_attribute('data-block-id') == surface_tail
    # A cloned authoritative replay must not lose the reader's location.
    before = page.evaluate('readingSnapshot()')
    page.evaluate('surface.render(selectConversationViewModel(JSON.parse(JSON.stringify(fixtureState))))')
    after = page.evaluate('readingSnapshot()')
    assert after['anchor'] == before['anchor']
    assert abs(after['y'] - before['y']) <= 2
    path = tmp_path / f'scroll-history-{history_kind}-{width}'
    page.screenshot(path=str(path.with_suffix('.png')))
    path.with_suffix('.json').write_text(json.dumps(measurements, indent=2) + '\n')
    print(f'Scroll history evidence: {path}')
    page.evaluate('surface.dispose()')


@pytest.mark.parametrize('width', [1180, 390])
def test_shipped_latest_button_restores_history_window_and_scoped_gestures(
        browser, live_server, tmp_path, width):
    """Exercise the actual delegated button and retained bridge on a local app."""
    from playwright.sync_api import expect
    from tests.conftest import _dismiss_onboarding_modals

    cid = 'scroll-owner-' + uuid.uuid4().hex[:10]
    context = browser.new_context(viewport={'width': width, 'height': 800}, service_workers='block')
    context.add_init_script(f"sessionStorage.setItem('tofu_activeConvId', '{cid}');")
    # Listener counts measure ownership across real navigation, not source text.
    context.add_init_script("""(() => {
      const add=EventTarget.prototype.addEventListener, remove=EventTarget.prototype.removeEventListener;
      const active=new Map(); window.chatGestureCounts=()=>Object.fromEntries(
        [...active].map(([type,listeners])=>[type,listeners.size]));
      EventTarget.prototype.addEventListener=function(type,callback,options) {
        if (this.id==='chatContainer' && ['wheel','touchstart','touchmove','keydown'].includes(type)) {
          if (!active.has(type)) active.set(type,new Set()); active.get(type).add(callback);
        }
        return add.call(this,type,callback,options);
      };
      EventTarget.prototype.removeEventListener=function(type,callback,options) {
        if (this.id==='chatContainer') active.get(type)?.delete(callback);
        return remove.call(this,type,callback,options);
      };
    })();""")
    page = context.new_page()
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    try:
        for index in range(26):
            text = f'Synthetic reading item {index}. ' + 'Stable reading evidence. ' * 60
            body = {'commandId': f'{cid}-{index}', 'actor': 'assistant', 'kind': 'text',
                    'projection': {'content': text, 'segments': [{'type': 'text',
                        'blockId': f'answer-{index}', 'text': text, 'terminal': True, 'deliverable': True}]}}
            if index == 0:
                body['conversation'] = {'allowCreate': True, 'title': cid, 'createdAt': 100, 'settings': {}}
            response = context.request.post(f'{live_server}/api/v4/conversations/{cid}/turns/settled', data=body)
            assert response.status == 200, response.text()
        page.goto(live_server, wait_until='domcontentloaded')
        _dismiss_onboarding_modals(page)
        surface = page.locator(f'#chatInner .conversation-surface[data-conversation-id="{cid}"]')
        expect(surface).to_be_visible(timeout=20000)
        expect(surface).to_contain_text('Synthetic reading item 25.')
        first = surface.locator('[data-turn-id]').first.get_attribute('data-turn-id')
        page.locator('#chatContainer').hover()
        page.mouse.wheel(0, -100000)
        page.wait_for_function("""first => document.querySelector('#chatInner [data-turn-id]')
          ?.dataset.turnId !== first""", arg=first)
        expect(surface).to_have_attribute('data-has-later-content', 'true')
        expect(page.locator('#scrollToBottomBtn')).to_be_visible()
        page.locator('#scrollToBottomBtn').click()
        expect(surface).to_contain_text('Synthetic reading item 25.')
        expect(surface).to_have_attribute('data-has-later-content', 'false')
        page.wait_for_function("""() => {const v=document.querySelector('#chatContainer');
          return v.scrollHeight-v.scrollTop-v.clientHeight<2;}""")
        counts = page.evaluate('chatGestureCounts()')
        assert counts == {'wheel': 2, 'touchstart': 1, 'touchmove': 1, 'keydown': 1}, counts
        assert surface.locator('[data-turn-id]').count() <= 80
        page.screenshot(path=str(tmp_path / f'shipped-latest-{width}.png'))
        assert not errors, errors
    finally:
        context.request.delete(f'{live_server}/api/v4/conversations/{cid}')
        context.close()
