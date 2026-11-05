"""Context rail travel across rounds through its production Surface.

Rendered through the shipped stylesheet, Surface, classic renderers and the
real rail track pass in Chromium.

1. Shape of conversation mvk7np4uiwkpv2: a railed human round, then a
   proactive (定时代理) round persisted before server-created inputs recorded
   a context snapshot, then a later railed round. Protects: the card keeps
   following beside a round that has no card of its own (no empty gutter),
   and still yields exactly at the next railed round's host (never
   trespassing into a round that has its own card).
2. A running round whose card holds more facts than the pane can show
   (2026-11-01 owner report: mid-round, the bottom of the card could not be
   reached until the transcript was scrolled to its end, and the following
   felt weird). Protects: the card is pinned in the same frame as the
   scroll, holds its pin in every frame while a followed stream grows the
   transcript, stays inside the pane wherever the reader is in the round,
   scrolls within itself to its last fact without moving the transcript,
   keeps its head in view at the transcript's tail, and its track never
   extends the transcript's scroll range.
"""
import json

import pytest

from tests._runtime_sections import ROOT, runtime_section
from tests.support.frontend_visual import frontend_visual_origin, frontend_visual_page as frontend_visual_page  # noqa: F401
from tests.visual.test_visual_timeline_reading import timeline_bundle as timeline_bundle

pytestmark = pytest.mark.visual

_TOP_EDGE_RANGE_PX = 12

_MEASURE = r"""() => {
  const pane = document.querySelector('.chat-container');
  const p = pane.getBoundingClientRect();
  const rails = {};
  for (const rail of document.querySelectorAll('.conversation-turn > .turn-ctx')) {
    if (rail.hidden) continue;
    const card = rail.querySelector(':scope > .tctx-card');
    const r = card.getBoundingClientRect();
    const top = Math.max(r.top, p.top), bottom = Math.min(r.bottom, p.bottom);
    const hit = bottom - top > 4 ? document.elementFromPoint(r.left + r.width / 2, (top + bottom) / 2) : null;
    const last = [...card.querySelectorAll('.tctx-todo-item')].at(-1)?.getBoundingClientRect();
    rails[rail.parentElement.dataset.turnId] = {top: r.top - p.top, bottom: r.bottom - p.top,
      painted: !!(hit && rail.contains(hit)), scrollTop: card.scrollTop,
      pointer: [r.left + r.width / 2, (top + bottom) / 2],
      scrollHeight: card.scrollHeight, clientHeight: card.clientHeight,
      lastTop: last ? last.top - p.top : null, lastBottom: last ? last.bottom - p.top : null};
  }
  const hosts = {};
  for (const node of document.querySelectorAll('.conversation-turn')) {
    const r = node.getBoundingClientRect();
    hosts[node.dataset.turnId] = {top: r.top - p.top, docTop: r.top - p.top + pane.scrollTop};
  }
  const transcript = document.getElementById('chatInner').getBoundingClientRect();
  return {rails, hosts, paneHeight: pane.clientHeight, scrollTop: pane.scrollTop,
    scrollHeight: pane.scrollHeight, transcriptEnd: transcript.bottom - p.top + pane.scrollTop};
}"""

#: The first conversation: a railed round, a snapshot-less proactive round,
#: and the next railed round.
_PROACTIVE_ROUNDS = r"""[
  turn('human',0,'human',{content:'排查 kunlunbedrock 的支持方式。',contextSnapshot:context('claude-opus-5.5')}),
  turn('reply',1,'assistant',reply('reply-text',60)),
  turn('proactive',2,'human',{content:'后台命令已完成，请审阅结果。',origin:{blockId:'origin',initiator:'proactive'}}),
  turn('proactive-reply',3,'assistant',reply('proactive-text',60)),
  turn('next',4,'human',{content:'继续。',contextSnapshot:context('gpt-6')}),
  turn('next-reply',5,'assistant',reply('next-text',60)),
]"""

#: The second conversation: one running round whose card carries a full
#: catalog of tools, the workspace, the branch and a long plan.
_TALL_RUNNING_ROUND = r"""[
  turn('human',0,'human',{content:'实现共享的工具目录策略并跑完测试阶梯。',contextSnapshot:{blockId:'turn-context',snapshot:{
    ...context('claude-opus-5.5').snapshot,
    tools:['Search ×N','Fetch','Code Exec','Memory','MCP: github ×26','MCP: hope ×63','MCP: docs ×8',
      'MCP: browser ×12','MCP: calendar ×4','MCP: research ×7','MCP: workspace ×3','MCP: lark ×9']
      .map(label=>({label,tone:label.startsWith('MCP')?'mcp':'net'})),
    git:{branch:'eval/opencode-glm53-tb21'},
    todos:Array.from({length:9},(_,i)=>({content:`第 ${i+1} 项：检查工具缓存的所有者、契约与当前改动，整理内置工具清单`,
      status:i<3?'completed':i===3?'in_progress':'pending'}))}}}),
  {...turn('reply',1,'assistant',reply('reply-text',90)),status:'running'},
]"""


def _settle(page):
    page.evaluate('() => new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)))')


def _scroll(page, y):
    page.evaluate("(y) => { document.querySelector('.chat-container').scrollTop = y; }", y)
    page.wait_for_timeout(60)
    _settle(page)
    return page.evaluate(_MEASURE)


def _boot(page, timeline_bundle, *, viewport, turns, expand=False):
    page.set_viewport_size(viewport)
    page.emulate_media(reduced_motion='reduce')
    page.set_content('<!doctype html><html data-theme="tofu"><head>'
                     '<link rel="stylesheet" href="/static/styles.css"></head><body></body></html>')
    page.add_style_tag(path=str(ROOT / 'frontend/src/conversation/ui/conversation-surface.css'))
    page.evaluate("""html=>{
      const shell=new DOMParser().parseFromString(html,'text/html');
      shell.querySelectorAll('script').forEach(n=>n.remove());
      document.body.replaceChildren(...shell.body.childNodes);
      document.getElementById('chatInner').innerHTML='<main id="surface" style="display:contents"></main>';
      const geometry=getComputedStyle(document.getElementById('chatInner'));
      for(const key of ['--rail-w','--rail-gap'])document.querySelector('.input-inner').style.setProperty(key,geometry.getPropertyValue(key));
    }""", (ROOT / 'index.html').read_text())
    locale = json.loads((ROOT / 'frontend/src/i18n/locales/zh.json').read_text())
    page.evaluate(r"""(locale)=>{
      window.runtimeScope=window; window.BASE_PATH=''; window.projectState={active:false}; window._i18nLang='zh';
      window.t=(key,values={})=>(locale[key]||key).replace(/\{(\w+)\}/g,(m,k)=>values[k]??m);
      window.escapeHtml=value=>String(value??'').replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;').replaceAll('"','&quot;');
      window._modelShortName=model=>model;
    }""", locale)
    for source in ['node_modules/marked/lib/marked.umd.js', 'node_modules/dompurify/dist/purify.min.js']:
        page.add_script_tag(path=str(ROOT / source))
    page.add_script_tag(content=runtime_section('core/markdown.js', scope_prelude=False))
    page.add_script_tag(content=runtime_section('info-rail.js', scope_prelude=False))
    page.add_script_tag(path=timeline_bundle)
    page.evaluate(r"""async ({locale,expand})=>{
      await setReadingLanguage('zh');
      const context=model=>({blockId:'turn-context',snapshot:{model,depth:'max',
        tools:[{label:'Search ×N',tone:'search'},{label:'Fetch',tone:'net'},{label:'Code Exec',tone:'code'}],
        roots:[{short:'your-username/tofu',path:'/workspace/your-username/tofu',ro:false}]}});
      const long=n=>Array.from({length:n},(_,i)=>`第 ${i+1} 段：后台命令完成后，代理继续审阅结果并整合修复。`).join('\n\n');
      const turn=(turnId,ordinal,actor,projection)=>({turnId,conversationId:'rail-follow',role:actor==='human'?'user':'assistant',actor,
        laneId:'main',ordinal,status:'completed',projectionRevision:1,projection});
      const reply=(id,paragraphs)=>({segments:[{type:'text',blockId:id,deliverable:true,terminal:true,text:long(paragraphs)}]});
      const turns=__TURNS__;
      window.state={...createTurnState('rail-follow'),turnsById:Object.fromEntries(turns.map(t=>[t.turnId,t])),laneOrder:{main:turns.map(t=>t.turnId)}};
      window.surface=createConversationSurface(document.getElementById('surface'),createClassicConversationRenderers({
        renderSafeMarkdownHtml:renderMarkdown,localizedText:(key,fallback,values={})=>(locale[key]||fallback).replace(/\{(\w+)\}/g,(m,k)=>values[k]??m),
        roleLabel:turn=>turn.role==='user'?'你':'Tofu',renderTurnContextParts:block=>renderTurnCtxNote(block.value.snapshot)}));
      surface.render(selectConversationViewModel(state));
      if(expand)document.querySelectorAll('.turn-ctx [data-tctx-more]').forEach(button=>button.click());
    }""".replace('__TURNS__', turns), {'locale': locale, 'expand': expand})
    _settle(page)
    return page.evaluate(_MEASURE)['hosts']


def _assert_pinned(state, turn):
    rail = state['rails'][turn]
    assert 0 <= rail['top'] <= _TOP_EDGE_RANGE_PX, (turn, state)
    assert rail['painted'], f'{turn} is positioned but not painted on top: {state}'


def test_card_follows_beside_a_round_without_its_own_card(frontend_visual_page, timeline_bundle, tmp_path):
    page = frontend_visual_page
    hosts = _boot(page, timeline_bundle, viewport={'width': 1680, 'height': 1000}, turns=_PROACTIVE_ROUNDS)
    assert set(page.evaluate(_MEASURE)['rails']) == {'human', 'next'}

    state = _scroll(page, (hosts['human']['docTop'] + hosts['proactive']['docTop']) / 2)
    _assert_pinned(state, 'human')

    # The reported gap: past the proactive input the gutter used to be empty.
    for y in (hosts['proactive']['docTop'] + 40, hosts['proactive-reply']['docTop'] + 1200):
        state = _scroll(page, y)
        _assert_pinned(state, 'human')
    page.screenshot(path=str(tmp_path / 'proactive-round.png'))

    # The next railed round still ends the card exactly at its host.
    state = _scroll(page, hosts['next']['docTop'] - 120)
    assert state['rails']['human']['bottom'] <= state['hosts']['next']['top'] + 1, state
    assert state['rails']['next']['painted'], state

    state = _scroll(page, hosts['next-reply']['docTop'] + 1200)
    _assert_pinned(state, 'next')
    assert state['rails']['human']['bottom'] <= 0, state


def test_tall_card_stays_in_the_pane_and_scrolls_within_itself(frontend_visual_page, timeline_bundle, tmp_path):
    page = frontend_visual_page
    hosts = _boot(page, timeline_bundle, viewport={'width': 1680, 'height': 760},
                  turns=_TALL_RUNNING_ROUND, expand=True)
    state = page.evaluate(_MEASURE)
    assert abs(state['scrollHeight'] - state['transcriptEnd']) <= 1, (
        f'the rail track extends the transcript scroll range: {state}')

    # Mid-round: pinned, inside the pane, and holding more than the pane shows.
    state = _scroll(page, hosts['human']['docTop'] + 1200)
    _assert_pinned(state, 'human')
    card = state['rails']['human']
    assert card['bottom'] <= state['paneHeight'], (
        f'the card hangs below the pane, where nothing can reveal it: {state}')
    assert card['scrollHeight'] > card['clientHeight'] + 1, (
        f'fixture must hold more facts than the pane can show: {card}')
    assert card['lastBottom'] > card['bottom'], (
        f'fixture must start with the last fact out of view: {card}')

    # The browser pins the card in the frame of the scroll itself: geometry
    # read synchronously after moving the transcript, before any frame could
    # run a script, already has the card pinned.
    now = page.evaluate("""(y) => {
      document.querySelector('.chat-container').scrollTop = y;
      return (%s)();
    }""" % _MEASURE, hosts['human']['docTop'] + 2400)
    _assert_pinned(now, 'human')

    # The transcript's tail keeps the card's head in view.
    state = _scroll(page, 10 ** 7)
    _assert_pinned(state, 'human')
    assert state['rails']['human']['bottom'] <= state['paneHeight'], state
    page.screenshot(path=str(tmp_path / 'tall-card-tail.png'))

    # Mid-round again: scrolling over the card reaches its last fact and
    # leaves the transcript where the reader left it.
    state = _scroll(page, hosts['human']['docTop'] + 1200)
    card = state['rails']['human']
    page.mouse.move(*card['pointer'])
    page.mouse.wheel(0, card['scrollHeight'])
    page.wait_for_timeout(400)
    after = page.evaluate(_MEASURE)
    card = after['rails']['human']
    assert after['scrollTop'] == state['scrollTop'], (
        f'scrolling the card moved the transcript: {state["scrollTop"]} -> {after["scrollTop"]}')
    assert card['scrollTop'] >= card['scrollHeight'] - card['clientHeight'] - 1, (
        f'the card could not be scrolled to its end: {card}')
    assert card['top'] <= card['lastTop'] and card['lastBottom'] <= card['bottom'] + 1, (
        f'the last fact is still out of view after scrolling the card: {card}')
    page.screenshot(path=str(tmp_path / 'tall-card-scrolled.png'))


#: Each frame: record where the card painted in the previous frame, then grow
#: the reply and follow the transcript's end in the same frame, as a stream
#: does (the Surface commits and follows inside one animation frame).
_FOLLOW_A_GROWING_STREAM = r"""async (frames) => {
  const pane = document.querySelector('.chat-container');
  const reply = document.querySelector('[data-block-id="reply-text"] .md-content');
  const card = document.querySelector('[data-turn-id="human"] > .turn-ctx > .tctx-card');
  pane.scrollTop = pane.scrollHeight;
  const tops = [];
  await new Promise((resolve) => {
    const tick = () => {
      tops.push(card.getBoundingClientRect().top - pane.getBoundingClientRect().top);
      if (tops.length > frames) { resolve(); return; }
      const paragraph = document.createElement('p');
      paragraph.textContent = '流式追加：代理继续审阅结果并整合修复，记录每个改动的证据。'.repeat(3);
      reply.append(paragraph);
      pane.scrollTop = pane.scrollHeight;
      requestAnimationFrame(tick);
    };
    requestAnimationFrame(tick);
  });
  const transcript = document.getElementById('chatInner').getBoundingClientRect();
  return {tops, scrollHeight: pane.scrollHeight,
    transcriptEnd: transcript.bottom - pane.getBoundingClientRect().top + pane.scrollTop};
}"""


def test_tall_card_holds_still_while_a_followed_stream_grows(frontend_visual_page, timeline_bundle):
    page = frontend_visual_page
    _boot(page, timeline_bundle, viewport={'width': 1680, 'height': 760},
          turns=_TALL_RUNNING_ROUND, expand=True)
    _scroll(page, 10 ** 7)
    result = page.evaluate(_FOLLOW_A_GROWING_STREAM, 24)
    # A track lengthened a frame late lets each growth paint the pane-tall
    # card pushed up by the growth, then snap back: a twitch per token.
    moved = [top for top in result['tops'] if not 0 <= top <= _TOP_EDGE_RANGE_PX]
    assert not moved, f'the card left its pin while the stream grew: {result["tops"]}'
    assert abs(result['scrollHeight'] - result['transcriptEnd']) <= 1, (
        f'the rail track extends the transcript scroll range: {result}')
