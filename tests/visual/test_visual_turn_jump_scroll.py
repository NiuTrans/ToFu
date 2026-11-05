"""Round jumps must reach rendered Turns, not merely call a scroll callback.

Uses the shipped navigation bridge, typed Surface and native Chromium layout on
an isolated local HTTP origin. Layout cases use synthetic directory/history
ports; the shipped-app cases create and delete their own synthetic conversation
through the real API in isolated test storage. No user conversations are used.
"""
import json
import threading
import time

import pytest

from tests._runtime_sections import ROOT, native_module_path, runtime_section
from tests.support.frontend_visual import (
    frontend_visual_origin as frontend_visual_origin,
    frontend_visual_page as frontend_visual_page,
)
from tests.turn_lifecycle.test_task_event_delivery import owner as owner, _discard

pytestmark = pytest.mark.visual


@pytest.fixture(scope='module')
def turn_jump_modules():
    return [native_module_path(f'.native/turn-jump-{name}.js', ROOT / source)
            for name, source in [
                ('surface', 'frontend/src/conversation/index.ts'),
                ('state', 'frontend/src/core/turn-state.ts'),
                ('navigation', 'frontend/src/features/conversation-bar/turn-navigation.ts'),
            ]]


def _mount(page, modules, motion):
    page.emulate_media(reduced_motion=motion)
    page.evaluate("""() => {
      document.body.innerHTML='<main id="chatContainer"><div id="chatInner"></div></main>'
        +'<nav id="turnNav"></nav>';
      document.body.style.cssText='display:block;margin:0;padding:0';
      document.querySelector('#chatContainer').style.cssText=
        'height:700px;overflow:auto;overflow-anchor:none;scroll-behavior:auto';
      document.querySelector('#turnNav').style.cssText=
        'position:fixed;right:12px;bottom:12px;z-index:1000';
    }""")
    for module in modules:
        page.add_script_tag(path=module)
    page.evaluate(r"""() => {
      const cid='synthetic-turn-jump';
      window.fixtureConv={id:cid};
      const makeTurn=i=>({turnId:'turn-'+i,conversationId:cid,laneId:'main',
        ordinal:i,actor:i%2===0?'human':'assistant',kind:i%2===0?'input':'reply',
        status:'completed',projectionRevision:1,currentAttemptId:null,
        createdAt:i,updatedAt:i,settlement:{outcome:'completed'},projection:{segments:[{
          type:'text',blockId:'turn-'+i+':answer',terminal:true,deliverable:true,
          text:`Synthetic turn ${i}\n\n`+'Readable history evidence. '.repeat(i%2===0?12:100)
        }]}});
      window.allTurns=Array.from({length:300},(_,i)=>makeTurn(i));
      window.humanTurns=allTurns.filter(turn=>turn.actor==='human');
      window.fixtureState={...createTurnState(cid),
        turnsById:Object.fromEntries(allTurns.slice(-64).map(turn=>[turn.turnId,turn])),
        laneOrder:{main:allTurns.slice(-64).map(turn=>turn.turnId)},
        historyByLane:{main:{hasMore:true,nextBeforeOrdinal:236,totalTurns:300}}};
      window.historyRequests=[];window.hydrated=[];window.jumpRequests=[];
      window.fixtureRepaint=()=>surface.render(selectConversationViewModel(fixtureState));
      window.acceptTurns=turns=>{
        const records={...fixtureState.turnsById,
          ...Object.fromEntries(turns.map(turn=>[turn.turnId,turn]))};
        const order=Object.values(records).sort((a,b)=>a.ordinal-b.ordinal);
        fixtureState={...fixtureState,turnsById:records,
          laneOrder:{main:order.map(turn=>turn.turnId)},
          historyByLane:{main:{hasMore:order[0].ordinal>0,
            nextBeforeOrdinal:order[0].ordinal,totalTurns:300}}};
        fixtureRepaint();
      };
      window.surface=createConversationSurface(document.querySelector('#chatInner'),{
        scrollViewport:document.querySelector('#chatContainer'),
        windowing:{maxTurns:20,batchSize:5},
        onIntent:async intent=>{
          if(intent.type!=='load-earlier-turns')return;
          historyRequests.push(intent.beforeOrdinal);
          acceptTurns(allTurns.slice(Math.max(0,intent.beforeOrdinal-32),intent.beforeOrdinal));
        }});
      window.runtimeScope={
        ConversationSurfacePresentation:{revealTurn:(id,turnId)=>
          id===cid&&surface.revealTurn(turnId)},
        requestAuthoritativeConversationRender:()=>{fixtureRepaint();return true;},
        ConversationTurnStore:{
          ensureRuntimeStore:()=>({getState:()=>fixtureState}),
          roundDirectoryKey:()=> 'synthetic-owner:head-1',
          loadConversationRoundDirectory:async(conv,lane,offset,limit,anchor)=>({
            ownerId:1,conversationId:cid,laneId:'main',syncSeq:1,stale:false,
            offset,totalRounds:humanTurns.length,
            activeRound:anchor?humanTurns.findIndex(turn=>turn.turnId===anchor)+1:humanTurns.length,
            entries:humanTurns.slice(offset,offset+limit).map(turn=>({
              turnId:turn.turnId,ordinal:turn.ordinal,actor:turn.actor,
              roundNumber:turn.ordinal/2+1}))}),
          hydrateConversationRound:async(conv,lane,entry)=>{
            hydrated.push(entry.turnId);
            if(!fixtureState.turnsById[entry.turnId]){
              acceptTurns(allTurns.slice(entry.ordinal,entry.ordinal+32));
            }
          },
          loadConversationTurnPage:async()=>{throw new Error('Unexpected navigation paging');}
        }};
      window.getActiveConv=()=>fixtureConv;
      window._getChatContainer=()=>document.querySelector('#chatContainer');
      window.prefersReducedMotion=()=>matchMedia('(prefers-reduced-motion: reduce)').matches;
      window.t=(key,params)=>params?`${params.current} / ${params.total}`:key;
      window.jumpSnapshot=round=>{
        const viewport=document.querySelector('#chatContainer');
        const target=document.querySelector(`#chatInner [data-turn-id="turn-${(round-1)*2}"]`);
        return {round,top:viewport.scrollTop,height:viewport.scrollHeight,
          offset:target?target.getBoundingClientRect().top-viewport.getBoundingClientRect().top:null,
          mounted:surface.mountedTurnIds.length,historyRequests:[...historyRequests],
          documentTop:document.scrollingElement.scrollTop};
      };
    }""")
    page.add_script_tag(content=runtime_section('ui/turn_nav.js', scope_prelude=False))
    page.evaluate("""() => {
      fixtureRepaint();buildTurnNav(fixtureConv);
      document.querySelector('#chatContainer').addEventListener('scroll',updateActiveTurn);
      // A normal authoritative repaint while the jump is moving. Every commit
      // restores the reader's anchor, which used to abort the native animation.
      window.repaintDuringJump=()=>{
        let remaining=8;
        const paint=()=>{fixtureRepaint();if(--remaining)requestAnimationFrame(paint);};
        requestAnimationFrame(paint);
      };
    }""")
    page.wait_for_function("""() => {
      const viewport=document.querySelector('#chatContainer');
      return document.querySelector('#turnNav').getAttribute('aria-busy')==='false'
        && viewport.scrollHeight-viewport.scrollTop-viewport.clientHeight<=2;
    }""")


def _jump(page, round):
    page.locator('.turn-nav-position').click()
    page.wait_for_function("document.querySelector('#turnNav').getAttribute('aria-busy')==='false'")
    page.locator('.turn-jump-direct input').fill(str(round))
    page.evaluate('repaintDuringJump()')
    page.locator('.turn-jump-direct input').press('Enter')
    page.wait_for_function("document.querySelector('#turnNav').getAttribute('aria-busy')==='false'")
    # Bound native animation/late layout observation without accepting the
    # navigation readout as evidence that the actual target was reached.
    return page.evaluate("""async round => {
      for(let i=0;i<60;i++)await new Promise(requestAnimationFrame);
      return jumpSnapshot(round);
    }""", round)


@pytest.mark.parametrize('width', [1180, 390])
@pytest.mark.parametrize('round_number', [135, 20])
@pytest.mark.parametrize('motion', ['no-preference', 'reduce'])
def test_round_jump_reaches_target_through_lazy_history_and_repaint(
        frontend_visual_page, turn_jump_modules, tmp_path, width, round_number, motion):
    page = frontend_visual_page
    page.set_viewport_size({'width': width, 'height': 800})
    _mount(page, turn_jump_modules, motion)
    target_id = f'turn-{(round_number - 1) * 2}'
    assert page.locator(f'#chatInner [data-turn-id="{target_id}"]').count() == 0
    was_loaded = page.evaluate('id => Boolean(fixtureState.turnsById[id])', target_id)
    assert was_loaded is (round_number == 135)
    before = page.evaluate('jumpSnapshot(150)')
    after = _jump(page, round_number)
    evidence = tmp_path / f'turn-jump-{width}-{round_number}-{motion}'
    page.screenshot(path=str(evidence.with_suffix('.png')))
    evidence.with_suffix('.json').write_text(json.dumps({'before': before, 'after': after}, indent=2) + '\n')
    assert after['offset'] is not None and abs(after['offset']) <= 2, after
    assert after['documentTop'] == 0, after
    assert after['mounted'] <= 20, after
    assert after['historyRequests'] == [], after
    assert page.evaluate('hydrated.at(-1)') == target_id
    assert page.locator('.turn-jump-panel').count() == 0
    assert page.locator('.turn-nav-position').evaluate('(node)=>document.activeElement===node')
    # A repeated jump and the tail jump must work, not just the first hydration.
    again = _jump(page, round_number)
    assert abs(again['offset']) <= 2, again
    latest = _jump(page, 150)
    assert abs(latest['offset']) <= 2, latest
    page.evaluate('surface.dispose()')


def test_tail_alignment_matches_native_scroll_range(frontend_visual_page, turn_jump_modules, tmp_path):
    """A short final Turn cannot reach the top beyond the scroll area's end."""
    page = frontend_visual_page
    # Independent geometry: no app CSS, history, transport or streaming timing.
    page.set_content('<!doctype html><html><body style="margin:0"></body></html>')
    page.add_script_tag(path=turn_jump_modules[0])
    snapshot = page.evaluate("""async () => {
      const make = left => {
        const viewport=document.createElement('section');
        viewport.style.cssText=`position:fixed;left:${left}px;top:0;width:200px;height:200px;overflow:auto`;
        const root=document.createElement('article');
        root.innerHTML='<div style="height:1000px"></div>'
          +'<section data-turn-id="last" style="height:100px">Last Turn</section>'
          +'<div style="height:40px"></div>';
        viewport.append(root);document.body.append(viewport);
        return {viewport,root,target:root.querySelector('[data-turn-id]')};
      };
      const actual=make(0), native=make(240);
      const port=createConversationViewportPort(actual.viewport);
      try {
        native.target.scrollIntoView({block:'start',behavior:'instant'});
        const reached=port.scrollToTurn(actual.root,'last');
        for(let i=0;i<2;i++)await new Promise(requestAnimationFrame);
        return {reached,actual:actual.viewport.scrollTop,native:native.viewport.scrollTop,
          offset:actual.target.getBoundingClientRect().top-actual.viewport.getBoundingClientRect().top};
      } finally {port.dispose();}
    }""")
    assert snapshot == {'reached': True, 'actual': 940, 'native': 940, 'offset': 60}
    (tmp_path / 'native-tail-scroll-range.json').write_text(json.dumps(snapshot) + '\n')


def _seed_settled_rounds(context, live_server, cid):
    """Append 50 human/assistant rounds through the public settled-Turn API."""
    turn_ids = []
    for index in range(100):
        text = f'Synthetic navigation item {index}. ' + 'Exact reading evidence. ' * 35
        body = {'commandId': f'{cid}-{index}', 'actor': 'human' if index % 2 == 0 else 'assistant',
                'kind': 'text', 'projection': {'content': text, 'segments': [{
                    'type': 'text', 'blockId': f'{cid}-{index}:answer', 'text': text,
                    'terminal': True, 'deliverable': True}]}}
        if index == 0:
            body['conversation'] = {'allowCreate': True, 'title': cid, 'createdAt': 100, 'settings': {}}
        response = context.request.post(f'{live_server}/api/v4/conversations/{cid}/turns/settled', data=body)
        assert response.status == 200, response.text()
        turn_ids.append(response.json()['turn']['turnId'])
    return turn_ids


_JUMP_OUTCOME = """id => {
  const target=[...document.querySelectorAll('#chatInner [data-turn-id]')]
    .find(node=>node.dataset.turnId===id);
  const error=document.querySelector('.turn-jump-error')?.textContent || '';
  if (error) return 'error: '+error;
  return target && document.querySelector('#turnNav').getAttribute('aria-busy')==='false'
    ? 'reached' : '';
}"""


@pytest.mark.parametrize('width', [1180])
def test_round_jumps_land_while_the_conversation_streams(
        browser, live_server, owner, monkeypatch, tmp_path, width):
    """A streaming reply advances the sync head on every event. Rank and
    history reads race that head; every jump in a row (direct entry,
    First/Last, directory rows) must land on its exact round instead of
    stranding the panel in "history failed" or silently doing nothing."""
    import uuid

    from playwright.sync_api import expect
    from lib import turn_lifecycle
    from lib.tasks_pkg.manager.runtime import chat_task_runtime
    from tests.conftest import _dismiss_onboarding_modals
    from tests.turn_lifecycle.test_turn_event_carried_task_event import _running_task
    from tests.visual.test_visual_surfaces import _wait_ready

    # Commit every streamed delta so the head moves continuously, as a busy
    # agent turn (tool rounds, phases, coalesced text) does in production.
    monkeypatch.setenv('TOFU_TURN_DELTA_RECORD_MS', '0')
    cid = 'round-jump-stream-' + uuid.uuid4().hex[:10]
    context = browser.new_context(viewport={'width': width, 'height': 800}, service_workers='block')
    context.add_init_script(f"localStorage.setItem('tofu_onboarding_v1_done', '1');"
                            f"sessionStorage.setItem('tofu_activeConvId', '{cid}');")
    page = context.new_page()
    errors, directory_reads, outcomes = [], [], []
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.on('request', lambda request: directory_reads.append(request.url)
            if f'/conversations/{cid}/turns/rounds?' in request.url else None)
    reads = []

    def record_response(response):
        if f'/conversations/{cid}/turns/' not in response.url or response.request.method != 'GET':
            return
        try:
            body = response.json()
        except Exception:
            body = {}
        reads.append({'path': response.url.split('/turns/')[1][:90], 'status': response.status,
                      'stale': body.get('stale'), 'syncSeq': body.get('syncSeq'),
                      'current': body.get('currentSyncSeq')})

    page.on('response', record_response)
    stop, streamer, task = threading.Event(), None, None
    try:
        turn_ids = _seed_settled_rounds(context, live_server, cid)
        task_id = cid + '-task'
        created, source = _running_task(turn_lifecycle, None, cid, task_id)
        task = chat_task_runtime.create(user_id=1, task_id=task_id)
        task.update(source)
        task.update(convId=cid, _turnId=created['turn']['turnId'], _workerStartedAt=time.time())
        page.goto(live_server, wait_until='domcontentloaded')
        _wait_ready(page)
        _dismiss_onboarding_modals(page)
        surface = page.locator(f'#chatInner .conversation-surface[data-conversation-id="{cid}"]')
        expect(surface).to_be_visible(timeout=20000)
        expect(page.locator('.turn-nav-readout')).to_have_text('51 / 51', timeout=20000)
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

        streamer = threading.Thread(target=stream, name='round-jump-streamer', daemon=True)
        streamer.start()
        position, panel = page.locator('.turn-nav-position'), page.locator('.turn-jump-panel')
        go = page.locator('.turn-jump-go')
        # (round, how): direct entry, First/Last buttons and directory rows.
        for round_number, how in [(1, 'input'), (25, 'input'), (51, 'last'), (1, 'first'),
                                  (50, 'input'), (10, 'row'), (37, 'input'), (2, 'row'), (49, 'input')]:
            first_read, events_before = len(reads), events['recorded']
            position.click()
            expect(panel).to_be_visible()
            expect(go).to_be_enabled(timeout=20000)
            if how == 'input':
                page.locator('.turn-jump-direct input').fill(str(round_number))
                page.locator('.turn-jump-direct input').press('Enter')
            elif how in {'first', 'last'}:
                page.locator(f'.turn-jump-{how}').click()
            else:
                page.locator(f'.turn-jump-entry[data-round-number="{round_number}"]').click()
            target_id = (turn_ids[(round_number - 1) * 2] if round_number <= 50
                         else created['turn']['parentTurnId'])
            try:
                outcome = page.wait_for_function(_JUMP_OUTCOME, arg=target_id, timeout=20000).json_value()
            except Exception:
                outcome = 'stuck: ' + json.dumps(page.evaluate("""() => ({
                  panelOpen: !document.querySelector('.turn-jump-panel')?.hidden,
                  busy: document.querySelector('#turnNav').getAttribute('aria-busy'),
                  readout: document.querySelector('.turn-nav-readout')?.textContent,
                  rows: document.querySelectorAll('.turn-jump-entry').length,
                  error: document.querySelector('.turn-jump-error')?.textContent || ''})"""))
            outcomes.append({'round': round_number, 'how': how, 'outcome': outcome,
                             'events': events['recorded'] - events_before})
            assert outcome == 'reached', (outcomes, reads[first_read:])
            expect(panel).to_be_hidden()
            snapshot = page.evaluate("""async id => {
              for(let i=0;i<8;i++)await new Promise(requestAnimationFrame);
              const viewport=document.querySelector('#chatContainer');
              const target=[...document.querySelectorAll('#chatInner [data-turn-id]')]
                .find(node=>node.dataset.turnId===id);
              return {offset:target.getBoundingClientRect().top-viewport.getBoundingClientRect().top,
                scrollTop:viewport.scrollTop, maxScroll:viewport.scrollHeight-viewport.clientHeight,
                targetHeight:target.getBoundingClientRect().height, viewportHeight:viewport.clientHeight,
                readout:document.querySelector('.turn-nav-readout').textContent};
            }""", target_id)
            # Like native scrollIntoView(block='start'), a short last round
            # aligns as far as the scroll area permits. Keep the same 2px
            # tolerance; arbitrary drift before that boundary still fails.
            target_top = snapshot['scrollTop'] + snapshot['offset']
            expected_scroll = min(snapshot['maxScroll'], max(0, target_top))
            assert abs(snapshot['scrollTop'] - expected_scroll) <= 2, (round_number, snapshot)
            assert snapshot['offset'] < snapshot['viewportHeight'], (round_number, snapshot)
            assert snapshot['offset'] + snapshot['targetHeight'] > 0, (round_number, snapshot)
            assert snapshot['readout'] == f'{round_number} / 51', (round_number, snapshot)
        # The head really moved under every jump; the streamer never failed.
        assert not events['errors'], events['errors']
        assert all(item['events'] > 0 for item in outcomes), outcomes
        assert directory_reads
        assert not errors, errors
        (tmp_path / 'streaming-round-jump.json').write_text(json.dumps(outcomes, indent=2) + '\n')
    finally:
        stop.set()
        if streamer:
            streamer.join(5)
        if task:
            task['aborted'] = True
            _discard(task)
        context.request.delete(f'{live_server}/api/v4/conversations/{cid}')
        context.close()


@pytest.mark.parametrize('width', [1180, 390])
def test_shipped_round_jump_reaches_unloaded_history_in_real_app(browser, live_server, tmp_path, width):
    """Production minified UI plus the actual coordinator and history/rank APIs."""
    import uuid

    from playwright.sync_api import expect
    from tests.conftest import _dismiss_onboarding_modals
    from tests.visual.test_visual_surfaces import _wait_ready

    cid = 'round-jump-' + uuid.uuid4().hex[:10]
    context = browser.new_context(viewport={'width': width, 'height': 800}, service_workers='block')
    # This journey starts after the user's first-run setup. Onboarding opens
    # asynchronously on a keyless fixture and is covered by its own journey.
    context.add_init_script(f"localStorage.setItem('tofu_onboarding_v1_done', '1');"
                            f"sessionStorage.setItem('tofu_activeConvId', '{cid}');")
    page = context.new_page()
    errors, history_reads, directory_reads = [], [], []
    page.on('pageerror', lambda error: errors.append(str(error)))

    def record_read(request):
        if f'/conversations/{cid}/turns/history?' in request.url:
            history_reads.append(request.url)
        elif f'/conversations/{cid}/turns/rounds?' in request.url:
            directory_reads.append(request.url)

    page.on('request', record_read)
    try:
        turn_ids = _seed_settled_rounds(context, live_server, cid)
        page.goto(live_server, wait_until='domcontentloaded')
        _wait_ready(page)
        _dismiss_onboarding_modals(page)
        surface = page.locator(f'#chatInner .conversation-surface[data-conversation-id="{cid}"]')
        expect(surface).to_be_visible(timeout=20000)
        expect(surface).to_contain_text('Synthetic navigation item 99.')
        expect(page.locator('.turn-nav-readout')).to_have_text('50 / 50', timeout=20000)
        expect(page.locator('#turnNav')).to_have_attribute('aria-busy', 'false')
        # The selected early turn is outside the initial server history page.
        expect(surface.locator(f'[data-turn-id="{turn_ids[0]}"]')).to_have_count(0)
        snapshots = []
        for round_number in [1, 25, 1, 50]:
            page.locator('.turn-nav-position').click()
            expect(page.locator('#turnNav')).to_have_attribute('aria-busy', 'false')
            page.locator('.turn-jump-direct input').fill(str(round_number))
            page.locator('.turn-jump-direct input').press('Enter')
            target_id = turn_ids[(round_number - 1) * 2]
            expect(surface.locator(f'[data-turn-id="{target_id}"]')).to_have_count(1, timeout=20000)
            expect(page.locator('#turnNav')).to_have_attribute('aria-busy', 'false')
            snapshot = page.evaluate("""async id => {
              for(let i=0;i<8;i++)await new Promise(requestAnimationFrame);
              const viewport=document.querySelector('#chatContainer');
              const target=[...document.querySelectorAll('#chatInner [data-turn-id]')]
                .find(node=>node.dataset.turnId===id);
              return {id,offset:target.getBoundingClientRect().top-viewport.getBoundingClientRect().top,
                count:document.querySelectorAll('#chatInner [data-turn-id]').length,
                top:viewport.scrollTop,documentTop:document.scrollingElement.scrollTop};
            }""", target_id)
            assert abs(snapshot['offset']) <= 2, snapshot
            assert snapshot['count'] <= 80 and snapshot['documentTop'] == 0, snapshot
            snapshots.append(snapshot)
        assert history_reads, 'early round did not use the actual history hydration API'
        assert directory_reads, 'rank lookup did not use the actual directory API'
        assert not errors, errors
        page.screenshot(path=str(tmp_path / f'real-round-jump-{width}.png'))
        (tmp_path / f'real-round-jump-{width}.json').write_text(json.dumps(snapshots, indent=2) + '\n')
    finally:
        context.request.delete(f'{live_server}/api/v4/conversations/{cid}')
        context.close()
