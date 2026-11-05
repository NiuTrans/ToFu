"""Scroll-driven history with real layout and shipped styles, no durable writes.

The static local-server fixture loads production Surface/selector modules. This
spec covers geometry and DOM retirement; transport/retry belongs to live-app
journeys and coordinator tests, not a substitute transport in this fixture.
"""
import json
import threading
import time
import uuid

import pytest

from tests._runtime_sections import ROOT, native_module_path
from tests.support.frontend_visual import (  # noqa: F401
    frontend_visual_origin,
    frontend_visual_page as frontend_visual_page,
)
from tests.turn_lifecycle.test_task_event_delivery import owner as owner, _discard

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
        # Read upward until the latest Turns lie far enough below to retire.
        for _ in range(40):
            if surface.get_attribute('data-has-later-content') == 'true':
                break
            page.mouse.wheel(0, -100000)
            page.wait_for_timeout(100)
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


def test_scroll_up_loads_history_while_the_conversation_streams(
        browser, live_server, owner, monkeypatch, tmp_path):
    """A streaming reply commits a replay event every 50 ms, so the reader's
    applied head trails the server's on every history read, and through a
    remote proxy each page lands a round trip after storage read it. Wheeling
    up must still page in older Turns down to the first one, without
    history-page conflicts, without replacing the projection from a snapshot,
    and without leaving the boundary failed."""
    from playwright.sync_api import expect
    from lib import turn_lifecycle
    from lib.tasks_pkg.manager.runtime import chat_task_runtime
    from tests.conftest import _dismiss_onboarding_modals
    from tests.turn_lifecycle.test_turn_event_carried_task_event import _running_task

    monkeypatch.setenv('TOFU_TURN_DELTA_RECORD_MS', '0')
    cid = 'scroll-stream-' + uuid.uuid4().hex[:10]
    context = browser.new_context(viewport={'width': 1180, 'height': 800}, service_workers='block')
    context.add_init_script(f"localStorage.setItem('tofu_onboarding_v1_done', '1');"
                            f"sessionStorage.setItem('tofu_activeConvId', '{cid}');")
    page = context.new_page()
    network = context.new_cdp_session(page)
    network.send('Network.enable')
    network.send('Network.emulateNetworkConditions', {
        'offline': False, 'latency': 250, 'downloadThroughput': -1, 'uploadThroughput': -1})
    errors, reads, snapshots = [], [], []
    page.on('pageerror', lambda error: errors.append(str(error)))

    def record_response(response):
        if f'/conversations/{cid}/' not in response.url or response.request.method != 'GET':
            return
        if '/turns/history?' in response.url:
            reads.append({'status': response.status, 'at': time.monotonic()})
        elif '/sync?' in response.url:
            snapshots.append({'status': response.status, 'at': time.monotonic()})

    page.on('response', record_response)
    stop, streamer, task = threading.Event(), None, None
    try:
        for index in range(160):
            text = f'Streaming history item {index}. ' + 'Readable older evidence. ' * 40
            body = {'commandId': f'{cid}-{index}', 'actor': 'human' if index % 2 == 0 else 'assistant',
                    'kind': 'text', 'projection': {'content': text, 'segments': [{
                        'type': 'text', 'blockId': f'{cid}-{index}:answer', 'text': text,
                        'terminal': True, 'deliverable': True}]}}
            if index == 0:
                body['conversation'] = {'allowCreate': True, 'title': cid, 'createdAt': 100, 'settings': {}}
            response = context.request.post(f'{live_server}/api/v4/conversations/{cid}/turns/settled', data=body)
            assert response.status == 200, response.text()
        task_id = cid + '-task'
        created, source = _running_task(turn_lifecycle, None, cid, task_id)
        task = chat_task_runtime.create(user_id=1, task_id=task_id)
        task.update(source)
        task.update(convId=cid, _turnId=created['turn']['turnId'], _workerStartedAt=time.time())
        events = {'recorded': 0, 'errors': []}

        def stream():
            while not stop.is_set():
                try:
                    task['content'] = f'Streaming reply {events["recorded"] + 1}'
                    turn_lifecycle.record_task_event(task, {'type': 'delta', 'content': ' more'})
                    events['recorded'] += 1
                except Exception as error:  # surfaced by the assertion below
                    events['errors'].append(repr(error))
                    return
                stop.wait(0.05)

        streamer = threading.Thread(target=stream, name='scroll-history-streamer', daemon=True)
        streamer.start()
        page.goto(live_server, wait_until='domcontentloaded')
        _dismiss_onboarding_modals(page)
        surface = page.locator(f'#chatInner .conversation-surface[data-conversation-id="{cid}"]')
        expect(surface).to_be_visible(timeout=20000)
        expect(surface).to_contain_text('Streaming history item 159.', timeout=20000)
        initial_snapshots = len(snapshots)
        boundary_states = set()
        page.locator('#chatContainer').hover()
        deadline = time.monotonic() + 90
        reached = False
        while time.monotonic() < deadline:
            page.mouse.wheel(0, -4000)
            page.wait_for_timeout(120)
            state = page.evaluate("""() => ({
              first: document.querySelector('#chatInner [data-turn-id]')?.textContent?.includes(
                'Streaming history item 0.') || false,
              boundary: [...document.querySelectorAll('#chatInner [data-history-state]')]
                .map(node => node.dataset.historyState)})""")
            boundary_states.update(state['boundary'])
            if state['first']:
                reached = True
                break
        evidence = {'reached': reached, 'reads': reads, 'snapshots': snapshots,
                    'initialSnapshots': initial_snapshots, 'events': events['recorded'],
                    'boundaryStates': sorted(boundary_states)}
        (tmp_path / 'streaming-scroll-history.json').write_text(json.dumps(evidence, indent=2) + '\n')
        assert reached, evidence
        assert not events['errors'], events['errors']
        assert events['recorded'] > 20, evidence
        assert reads and all(read['status'] == 200 for read in reads), evidence
        assert len(snapshots) == initial_snapshots, evidence
        assert 'failed' not in boundary_states, evidence
        assert not errors, errors
    finally:
        stop.set()
        if streamer:
            streamer.join(5)
        if task:
            task['aborted'] = True
            _discard(task)
        context.request.delete(f'{live_server}/api/v4/conversations/{cid}')
        context.close()
