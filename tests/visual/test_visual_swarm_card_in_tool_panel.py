"""A parallel run inside the grouped tool panel is one call of that card.

Real Chromium, the shipped stylesheet and the production Surface, swarm push
reducers and swarm card. Synthetic frames stay inside a disposable iframe.
Owner report 2026-11-01: the swarm sheet sat on a second, rounded box inside
the card, and the same worker state read `tool_exec` or 调用工具中 depending
on which frame arrived last.
"""
import json

import pytest

from tests._runtime_sections import ROOT, native_module_path, runtime_section

from tests.support.frontend_visual import frontend_visual_origin, frontend_visual_page as frontend_visual_page  # noqa: F401

pytestmark = pytest.mark.visual

GEOMETRY = r"""() => {
  const panel = document.querySelector('.sw-panel');
  const block = panel.closest('.conversation-block');
  const style = getComputedStyle(block);
  const box = block.getBoundingClientRect();
  const left = box.left + parseFloat(style.borderLeftWidth) + parseFloat(style.paddingLeft);
  const right = box.right - parseFloat(style.borderRightWidth) - parseFloat(style.paddingRight);
  const rect = selector => { const node = panel.querySelector(selector);
    if (!node) return null; const r = node.getBoundingClientRect();
    return {left: r.left, right: r.right, width: r.width}; };
  const sheet = getComputedStyle(panel);
  const outcomes = [...panel.querySelectorAll('.sw-header-right > :is(.sw-status-pill, .sw-header-timer)')];
  const neighbours = [...document.querySelectorAll('.conversation-block[data-turn-zone="process"]')]
    .filter(node => node !== block && node.querySelector('[data-action-summary]'))
    .map(node => node.querySelector('[data-action-summary] :is(.conversation-tool-card__title, .ptool-text)')
      ?.getBoundingClientRect().left);
  const token = name => parseFloat(getComputedStyle(document.documentElement).getPropertyValue(name));
  return {
    glyphColumn: token('--space-4'), glyphGap: token('--space-2'), endLane: token('--space-6'),
    state: block.dataset.actionState,
    blockRadius: [style.borderTopLeftRadius, style.borderTopRightRadius,
      style.borderBottomLeftRadius, style.borderBottomRightRadius],
    sheet: {border: [sheet.borderTopWidth, sheet.borderRightWidth, sheet.borderBottomWidth, sheet.borderLeftWidth],
      background: sheet.backgroundColor, radius: sheet.borderTopLeftRadius},
    left, right,
    glyph: rect('.sw-header-icon'), title: rect('.sw-header-title'),
    outcomeRight: outcomes.length ? outcomes[outcomes.length - 1].getBoundingClientRect().right : null,
    agentRows: [...panel.querySelectorAll('.sw-agent > .sw-a-header')].map(node => node.getBoundingClientRect().left),
    agentGlyphs: [...panel.querySelectorAll('.sw-agent .sw-a-status-icon')].map(node => node.getBoundingClientRect().left),
    neighbours,
    overflow: panel.scrollWidth > panel.clientWidth + 1,
  };
}"""


@pytest.mark.parametrize('width', [1100, 390])
@pytest.mark.parametrize('theme', ['dark', 'light', 'tofu'])
def test_swarm_run_reads_as_one_call_of_the_tool_panel(frontend_visual_page, tmp_path, width, theme):
    page = frontend_visual_page
    page.set_viewport_size({'width': width, 'height': 900})
    page.evaluate("""() => {
      const frame = document.createElement('iframe');
      frame.id = 'swarm-card-fixture';
      frame.style.cssText = 'position:fixed;inset:0;width:100%;height:100%;z-index:99999;background:white';
      document.body.append(frame);
    }""")
    frame = page.frames[-1]
    frame.set_content('<!doctype html><html><body><main id="chat" style="padding:12px"></main></body></html>')
    frame.evaluate('theme=>document.documentElement.dataset.theme=theme', theme)
    frame.add_style_tag(path=str(ROOT / 'static/styles.css'))
    locale = json.loads((ROOT / 'frontend/src/i18n/locales/zh.json').read_text())
    frame.evaluate(r"""locale => {
      window.t = (key, params) => (locale[key] || key).replace(/\{(\w+)\}/g,
        (match, name) => params?.[name] ?? match);
      window.escapeHtml = s => String(s ?? '').replaceAll('&','&amp;').replaceAll('<','&lt;')
        .replaceAll('>','&gt;').replaceAll('"','&quot;');
      window.runtimeScope = {}; window._TOOL_DISPLAY = {};
      window.errorEnvelopeMessage = String;
    }""", locale)
    frame.add_script_tag(path=native_module_path(
        '.native/swarm-card-surface.js', ROOT / 'frontend/src/conversation/index.ts'))
    frame.add_script_tag(path=native_module_path(
        '.native/swarm-card-failure.js', ROOT / 'frontend/src/conversation/presentation/swarm-failure-presentation.ts'))
    frame.add_script_tag(path=native_module_path(
        '.native/swarm-card-receipts.js', ROOT / 'frontend/src/conversation/presentation/agent-message-receipts.ts'))
    frame.add_script_tag(content=runtime_section('ui/streaming_swarm_panel.js'))
    frame.add_script_tag(content=runtime_section('ui/swarm_live_projection.js'))
    frame.evaluate("""() => {
      _swDemandReconciliation = () => {};
      const call = (id, name, roundNum, extra = {}) => ({toolName: name, toolCallId: id, roundNum,
        llmRound: roundNum - 1, taskId: 'run', status: 'done', toolContent: 'ok', ...extra});
      const rounds = [
        call('read', 'read_files', 1),
        call('launch', 'spawn_agents', 2, {toolContent: JSON.stringify({swarm_run_id: 'run',
          agents: [{id: 'scout', role: 'coder'}, {id: 'review', role: 'general'}]})}),
        call('cmd', 'run_command', 3),
      ];
      const turn = {turnId:'turn',conversationId:'fixture',laneId:'main',actor:'assistant',kind:'reply',
        status:'running',ordinal:1,currentAttemptId:null,createdAt:Date.now(),updatedAt:Date.now(),
        projectionRevision:1,projection:{toolRounds:rounds,segments:rounds.map(round => ({type:'tool_use',
          blockId:'tool:'+round.toolCallId,id:round.toolCallId,name:round.toolName,taskId:'run',input:{},
          result:{status:'done',content:''}}))}};
      const state = {conversationId:'fixture',conversationRevision:1,transport:'live',turnsById:{turn},
        laneOrder:{main:['turn']},attemptsById:{},queueItems:[],pendingEventsByTurn:{},commandPending:{},liveRoundUsageByTurn:{}};
      const overlays = new Map();
      const surface = createConversationSurface(document.getElementById('chat'), createClassicConversationRenderers({
        renderSafeMarkdownHtml: escapeHtml,
        renderToolBlockHtml: block => block.round?.toolName === 'spawn_agents'
          ? _buildSwarmPanelHTML(block.round, [block.round]) : '',
      }));
      const paint = () => surface.render(selectConversationViewModel(controller.presentation.compose({id:'fixture'},state,state)));
      const controller = createSwarmPushPresentationController({
        translate: t, notifyFailure: () => {}, findConversation: id => id === 'fixture' ? {id} : null,
        readTurnState: () => state, readOverlay: (_conv, id) => overlays.get(id),
        upsertOverlay: (_conv, value) => { overlays.set(value.turnId, value); paint(); },
        removeOverlay: () => {}, hydrateConversation: () => null, attachAutoContinue() {},
        reducePhase: _handleSwarmPhase, reduceAgent: _handleSwarmAgent,
        debug: (_message, error) => { throw error; }, warn() {},
      });
      window.sendSwarm = frame => controller.handleFrame({channel:'swarm',conversationId:'fixture',
        taskId:'fixture',executionTaskId:'run',swarmId:'run',...frame});
      window.fixtureSurface = surface;
      sendSwarm({type:'swarm_phase',phase:'spawning',agents:[
        {id:'scout',role:'coder',model:'test-model',objective:'Map every caller of the card'},
        {id:'review',role:'general',model:'test-model',objective:'Review the layout'}]});
      sendSwarm({type:'swarm_agent_phase',agentId:'scout',phase:'running'});
      sendSwarm({type:'swarm_agent_phase',agentId:'review',phase:'running'});
    }""")
    live = frame.evaluate(GEOMETRY)
    assert live['state'] == 'active', 'the live run is the active row that bent the card before'
    _assert_one_call_of_the_card(live)

    # One worker state reads as one word, whichever frame delivered it last.
    pills = "() => [...document.querySelectorAll('.sw-agent .sw-a-phase-pill')].map(node => node.textContent)"
    frame.evaluate("sendSwarm({type:'swarm_agent_phase',agentId:'scout',phase:'tool_exec',status:'running'})")
    assert frame.evaluate(pills)[0] == locale['swarm.phase.tool_use']
    frame.evaluate("""sendSwarm({type:'swarm_agent_tool_call',agentId:'scout',callId:'read-1',
      toolName:'read_files',callStatus:'running',argsBrief:'23-reading-tool-panel.css'})""")
    assert frame.evaluate(pills)[0] == locale['swarm.phase.tool_use']
    frame.evaluate("sendSwarm({type:'swarm_agent_phase',agentId:'review',phase:'waiting_model',status:'running'})")
    assert frame.evaluate(pills)[1] == locale['swarm.phase.waitingModel']
    frame.evaluate("sendSwarm({type:'swarm_agent_phase',agentId:'review',phase:'writing',status:'running'})")
    assert frame.evaluate(pills)[1] == locale['swarm.phase.writing']
    _assert_one_call_of_the_card(frame.evaluate(GEOMETRY))
    screenshot = tmp_path / f'swarm-card-{theme}-{width}.png'
    page.locator('#swarm-card-fixture').screenshot(path=str(screenshot))
    print(f'Swarm card screenshot: {screenshot}')

    frame.evaluate("""() => {
      for (const agentId of ['scout', 'review']) {
        sendSwarm({type:'swarm_agent_complete',agentId,status:'done',preview:'Mapped.',elapsed:14181.5});
      }
      sendSwarm({type:'swarm_phase',phase:'complete'});
    }""")
    settled = frame.evaluate(GEOMETRY)
    assert settled['state'] != 'active'
    assert frame.evaluate(pills) == [locale['swarm.phase.complete']] * 2
    assert frame.evaluate("() => [...document.querySelectorAll('.sw-agent .sw-a-timer')].map(node => node.textContent)") \
        == ['236m22s'] * 2, 'a settled clock reads like the live one'
    _assert_one_call_of_the_card(settled)
    frame.evaluate('fixtureSurface.dispose()')


def _assert_one_call_of_the_card(geometry):
    """The run takes the card's call columns and paints no frame of its own."""
    assert geometry['blockRadius'] == ['0px'] * 4, 'card sides stay straight through the run'
    assert geometry['sheet'] == {'border': ['0px'] * 4, 'background': 'rgba(0, 0, 0, 0)', 'radius': '0px'}
    assert not geometry['overflow']
    text_edge = geometry['left'] + geometry['glyphColumn'] + geometry['glyphGap']
    assert geometry['glyph']['left'] == pytest.approx(geometry['left'], abs=0.5)
    assert geometry['glyph']['width'] == pytest.approx(geometry['glyphColumn'], abs=0.5)
    assert geometry['title']['left'] == pytest.approx(text_edge, abs=0.5)
    assert len(geometry['neighbours']) == 2
    assert geometry['neighbours'] == [pytest.approx(text_edge, abs=0.5)] * 2, 'same intent edge as its sibling calls'
    assert geometry['agentRows'] == [pytest.approx(text_edge, abs=0.5)] * 2, 'agents hang from the title'
    assert geometry['outcomeRight'] == pytest.approx(geometry['right'] - geometry['endLane'], abs=0.5)
