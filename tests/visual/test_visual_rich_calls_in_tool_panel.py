"""Rich tool calls inside the grouped tool panel are calls of that card.

Real Chromium, the shipped stylesheet, the production Surface and the
production presenters. Synthetic rounds stay inside a disposable iframe.
Owner report 2026-11-03: the tool search's matched-tool grid sat on a tinted,
rounded sheet inside the card ("not a tool that suits a background frame;
full width like spawn_agents reads better"), and the sheet's margins opened
gaps in the card's sides. Timer watches drew the same inset sheet, the
project, motion and memory receipts a tinted body under their rows, and
guidance questions, native rich forms and a search's vertical-source group
boxed themselves the same way; inside hung bodies, injection reports (a
sub-agent's update, a steer message, a background command), board cards and
commit files were boxed rows, and a search's group heads painted bands.
"""
import json

import pytest

from tests._runtime_sections import ROOT, native_module_path

from tests.support.frontend_visual import frontend_visual_origin, frontend_visual_page as frontend_visual_page  # noqa: F401

pytestmark = pytest.mark.visual

MATCHES = [
    {'type': 'tool_catalog_match', 'toolName': f'mcp__xuecheng__{name}', 'namespace': 'xuecheng',
     'snippet': f'[MCP:xuecheng] {snippet}',
     'arguments': [{'name': arg, 'type': kind, 'required': required} for arg, kind, required in args]}
    for name, snippet, args in [
        ('rename_xtable', 'Rename a data table in a document.', [('table_id', 'string', True), ('title', 'string', True)]),
        ('set_doc_secret_level', "Set a document's secret level to C2, C3, or C4.", [('doc', 'string', True), ('level', 'string', True)]),
        ('sort_xtable', 'Reorder a data table within its document. `to` is 1-based.', [('table_id', 'string', True), ('to', 'integer', True)]),
        ('transfer_doc_owner', 'Transfer document ownership. The space is resolved automatically from the first doc.', [('docs', 'array', True), ('target_owner_mis', 'string', True)]),
        ('set_doc_link_share', "Configure 'anyone with the link' sharing. Set enabled and choose a permission.", [('doc', 'string', True), ('enabled', 'boolean', True), ('permission', 'string', False)]),
        ('add_doc_comment', 'Post an unanchored document-level comment, or reply to one.', [('doc', 'string', True), ('text', 'string', True), ('parent_comment_id', 'integer', False)]),
        ('copy_xtable', 'Copy a data table to another document. Supports partial copy.', [('source_table_id', 'integer', True), ('target_parent_id', 'integer', True), ('target_type', 'integer', False), ('column_ids', 'array', False)]),
        ('create_wps_excel_embed', 'Create the special WPS Excel attachment bound to a document.', [('doc', 'string', True), ('height', 'integer', False)]),
    ]
]

NO_SHEET = {'border': ['0px'] * 4, 'background': 'rgba(0, 0, 0, 0)', 'radius': '0px',
            'margin': ['0px'] * 4, 'shadow': 'none'}

# Shared measurements: a call's block, the card's rows and the shared inks.
HELPERS = r"""() => {
  window.panelToken = name => parseFloat(getComputedStyle(document.documentElement).getPropertyValue(name));
  window.panelResolved = (host, property, value) => {
    const probe = document.createElement('span');
    probe.style.setProperty(property, value);
    host.append(probe);
    const resolved = getComputedStyle(probe).getPropertyValue(property);
    probe.remove();
    return resolved;
  };
  window.panelRect = node => {
    if (!node) return null;
    const r = node.getBoundingClientRect();
    return {left: r.left, right: r.right, top: r.top, bottom: r.bottom, width: r.width};
  };
  window.panelSheet = node => {
    const s = getComputedStyle(node);
    return {border: [s.borderTopWidth, s.borderRightWidth, s.borderBottomWidth, s.borderLeftWidth],
      background: s.backgroundColor, radius: s.borderTopLeftRadius,
      margin: [s.marginTop, s.marginRight, s.marginBottom, s.marginLeft], shadow: s.boxShadow};
  };
  window.panelBlock = block => {
    const style = getComputedStyle(block);
    const box = block.getBoundingClientRect();
    return {left: box.left + parseFloat(style.borderLeftWidth) + parseFloat(style.paddingLeft),
      right: box.right - parseFloat(style.borderRightWidth) - parseFloat(style.paddingRight),
      radius: [style.borderTopLeftRadius, style.borderTopRightRadius,
        style.borderBottomLeftRadius, style.borderBottomRightRadius],
      overflow: block.scrollWidth > block.clientWidth + 1};
  };
  window.panelCard = () => {
    const panel = document.querySelector('.conversation-tool-panel');
    const rows = [panel];
    for (let node = panel.nextElementSibling; node?.dataset.turnZone === 'process'; node = node.nextElementSibling) rows.push(node);
    return {
      seams: rows.slice(1).map((node, index) =>
        node.getBoundingClientRect().top - rows[index].getBoundingClientRect().bottom),
      intents: rows.slice(1).map(node => node.querySelector(
        '[data-action-summary] > :is(.conversation-tool-card__title, .ptool-text, .timer-watcher-label, .hg-title), '
        + '.sw-inbox-row-header > .ptool-text')?.getBoundingClientRect().left ?? null),
      glyphColumn: panelToken('--space-4'), glyphGap: panelToken('--space-2'),
      rowEnd: panelToken('--space-1'), endLane: panelToken('--space-6'),
    };
  };
}"""


def _mount(page, theme, width, modules):
    page.set_viewport_size({'width': width, 'height': 1100})
    page.evaluate("""() => {
      const frame = document.createElement('iframe');
      frame.id = 'rich-call-fixture';
      frame.style.cssText = 'position:fixed;inset:0;width:100%;height:100%;z-index:99999;background:white';
      document.body.append(frame);
    }""")
    frame = page.frames[-1]
    frame.set_content('<!doctype html><html lang="zh"><body><main id="chat" style="padding:12px"></main></body></html>')
    frame.evaluate('theme=>document.documentElement.dataset.theme=theme', theme)
    frame.add_style_tag(path=str(ROOT / 'static/styles.css'))
    locale = json.loads((ROOT / 'frontend/src/i18n/locales/zh.json').read_text())
    frame.evaluate(r"""locale => {
      window.fixtureLocale = locale;
      window.t = (key, params) => (locale[key] || key).replace(/\{(\w+)\}/g,
        (match, name) => params?.[name] ?? match);
      window.escapeHtml = s => String(s ?? '').replaceAll('&','&amp;').replaceAll('<','&lt;')
        .replaceAll('>','&gt;').replaceAll('"','&quot;');
      window.runtimeScope = {}; window._TOOL_DISPLAY = {};
      window.errorEnvelopeMessage = String;
    }""", locale)
    for module in ['frontend/src/conversation/index.ts', *modules]:
        name = module.removeprefix('frontend/src/').replace('/', '-').removesuffix('.ts')
        frame.add_script_tag(path=native_module_path(f'.native/rich-call-{name}.js', ROOT / module))
    frame.evaluate(HELPERS)
    # One surface over synthetic rounds; `bodies` maps a call ID to its HTML
    # and `sidecars` carries projection receipts such as injections.
    frame.evaluate(r"""() => {
      window.mountRounds = (rounds, bodies, ports = {}, sidecars = {}) => {
        const turn = {turnId:'turn',conversationId:'fixture',laneId:'main',actor:'assistant',kind:'reply',
          status:'running',ordinal:1,currentAttemptId:null,createdAt:Date.now(),updatedAt:Date.now(),
          projectionRevision:1,projection:{toolRounds:rounds,segments:rounds.map(round => ({type:'tool_use',
            blockId:'tool:'+round.toolCallId,id:round.toolCallId,name:round.toolName,taskId:'run',input:{},
            result:{status:'done',content:''}})),...sidecars}};
        const state = {conversationId:'fixture',conversationRevision:1,transport:'live',turnsById:{turn},
          laneOrder:{main:['turn']},attemptsById:{},queueItems:[],pendingEventsByTurn:{},commandPending:{},liveRoundUsageByTurn:{}};
        const surface = createConversationSurface(document.getElementById('chat'), createClassicConversationRenderers({
          renderSafeMarkdownHtml: escapeHtml,
          renderToolBlockHtml: block => bodies[block.round?.toolCallId]?.(block.round) ?? '',
          ...ports,
        }));
        surface.render(selectConversationViewModel(state));
        window.fixtureSurface = surface;
      };
      window.fixtureGlyph = '<svg viewBox="0 0 16 16" width="14" height="14" aria-hidden="true"><circle cx="8" cy="8" r="6" fill="none" stroke="currentColor"/></svg>';
      window.fixtureCall = (id, name, roundNum, extra = {}) => ({toolName: name, toolCallId: id, roundNum,
        llmRound: roundNum - 1, taskId: 'run', status: 'done', toolContent: 'ok', ...extra});
      window.fixtureRow = label =>
        `<div class="ptool-line"><span class="ptool-icon">${fixtureGlyph}</span><span class="ptool-text">${label}</span></div>`;
    }""")
    return frame


def _assert_card_rows(card, text_edge, count):
    assert card['seams'] == [pytest.approx(0, abs=0.5)] * len(card['seams']), \
        'no gap opens in the card sides between its rows'
    assert len(card['intents']) == count
    assert card['intents'] == [pytest.approx(text_edge, abs=0.5)] * count, 'every call shares one intent edge'


@pytest.mark.parametrize('width', [1100, 390])
@pytest.mark.parametrize('theme', ['dark', 'light', 'tofu'])
def test_tool_search_reads_as_one_call_of_the_tool_panel(frontend_visual_page, tmp_path, width, theme):
    frame = _mount(frontend_visual_page, theme, width,
                   ['frontend/src/conversation/presentation/tool-search-presentation.ts'])
    frame.evaluate("""matches => {
      const presentation = createToolSearchPresentation({translate: t, iconHtml: () => ''});
      mountRounds([
        fixtureCall('read', 'read_files', 1),
        fixtureCall('search', 'search_tools', 2, {query: 'read xuecheng document content', results: matches,
          toolSearchTotal: 48, toolSearchNextCursor: 'next'}),
        fixtureCall('cmd', 'run_command', 3),
        fixtureCall('note', 'read_notes', 4),
      ], {
        read: () => fixtureRow('read_files'),
        search: round => presentation.renderSearchHtml(round, round.results, {
          iconHtml: fixtureGlyph, queryHtml: 'read xuecheng document content · xuecheng', rightControlsHtml: ''}),
        cmd: () => fixtureRow('run_command'),
        // A body with its own block margins (here a paragraph's) must not
        // open a gap in the card's sides between rows.
        note: () => fixtureRow('read notes') + '<p>Synthetic body with its own margins.</p>',
      });
    }""", MATCHES)
    geometry = frame.evaluate(r"""() => {
      const sheet = document.querySelector('.ptool-tool-search-block');
      const block = panelBlock(sheet.closest('.conversation-block'));
      const header = sheet.querySelector('.ptool-tool-search-header');
      const count = header.querySelector('.ptool-tool-search-count');
      const countStyle = getComputedStyle(count);
      const outcomes = [...header.querySelectorAll(':scope > :is(.ptool-tool-search-count, .ptool-tool-search-more)')]
        .filter(node => getComputedStyle(node).display !== 'none');
      return {
        block, card: panelCard(), sheet: panelSheet(sheet),
        glyph: panelRect(header.querySelector(':scope > .ptool-icon')),
        title: panelRect(header.querySelector(':scope > .ptool-text')),
        titleFont: getComputedStyle(header.querySelector(':scope > .ptool-text')).fontFamily,
        count: {background: countStyle.backgroundColor, color: countStyle.color,
          font: countStyle.fontFamily, top: count.getBoundingClientRect().top},
        metaInk: panelResolved(header, 'color', 'var(--tool-row-meta-ink)'),
        outcomeRight: outcomes.length ? outcomes[outcomes.length - 1].getBoundingClientRect().right : null,
        results: panelRect(sheet.querySelector('.ptool-tool-search-results')),
        cards: [...sheet.querySelectorAll('.ptool-tool-search-card')].map(panelRect),
      };
    }""")
    screenshot = tmp_path / f'tool-search-panel-{theme}-{width}.png'
    frontend_visual_page.locator('#rich-call-fixture').screenshot(path=str(screenshot))
    print(f'Tool search panel screenshot: {screenshot}')

    block, card = geometry['block'], geometry['card']
    text_edge = block['left'] + card['glyphColumn'] + card['glyphGap']
    assert block['radius'] == ['0px'] * 4, 'card sides stay straight through the search'
    assert not block['overflow']
    assert geometry['sheet'] == NO_SHEET
    _assert_card_rows(card, text_edge, 4)
    assert geometry['glyph']['left'] == pytest.approx(block['left'], abs=0.5)
    assert geometry['glyph']['width'] == pytest.approx(card['glyphColumn'], abs=0.5)
    assert geometry['title']['left'] == pytest.approx(text_edge, abs=0.5)
    # The count is metadata on the rows' outcome column, not an accent chip.
    assert geometry['count']['background'] == 'rgba(0, 0, 0, 0)'
    assert geometry['count']['color'] == geometry['metaInk']
    assert geometry['count']['font'] == geometry['titleFont']
    assert geometry['count']['top'] < geometry['title']['bottom'], 'the count stays on the call row'
    assert geometry['outcomeRight'] == pytest.approx(block['right'] - card['endLane'], abs=0.5)
    # The matched tools hang from the title and fill the card to its edge.
    assert geometry['results']['left'] == pytest.approx(text_edge, abs=0.5)
    assert geometry['results']['right'] == pytest.approx(block['right'], abs=0.5)
    assert len(geometry['cards']) == 8
    assert min(item['left'] for item in geometry['cards']) == pytest.approx(text_edge, abs=0.5)
    assert max(item['right'] for item in geometry['cards']) == pytest.approx(block['right'], abs=0.5)
    frame.evaluate('fixtureSurface.dispose()')


@pytest.mark.parametrize('width', [1100, 390])
@pytest.mark.parametrize('theme', ['dark', 'light', 'tofu'])
def test_timer_watches_and_receipts_read_as_calls_of_the_tool_panel(frontend_visual_page, tmp_path, width, theme):
    frame = _mount(frontend_visual_page, theme, width, [
        'frontend/src/conversation/ui/checklist-card/index.ts',
        'frontend/src/conversation/ui/timer-card/index.ts',
        'frontend/src/conversation/ui/project-receipt/index.ts',
        'frontend/src/conversation/ui/motion-card/index.ts',
    ])
    frame.evaluate(r"""() => {
      const locale = fixtureLocale;
      const text = (key, fallback) => typeof fallback === 'object' && fallback
        ? (locale[key] || key).replace('{n}', fallback.n) : locale[key] || fallback || key;
      const icon = (name, size = 14) => fixtureGlyph.replaceAll('"14"', `"${size}"`);
      const timerPorts = {text, now: () => 10000, formatTime: () => '12:34:56', markdown: escapeHtml,
        demandCountdown: () => {}, icon};
      const projectPorts = {text, markdown: () => '', now: () => 1700001000000,
        absoluteTime: value => new Date(value).toISOString(), conversationTitle: () => 'Review implementation', icon};
      const todo = {todoExplanation: 'Status reports progress, not proof of success.',
        todos: ['completed', 'in_progress', 'pending'].map(status => ({status, content: 'Review ' + status})),
        todoDisplayUpdateCount: 1, todoDisplayHistory: [{operation: 'set', done: 0, total: 3}]};
      mountRounds([
        fixtureCall('read', 'read_files', 1),
        fixtureCall('todo', 'todo_write', 2),
        fixtureCall('watch', 'timer_create', 3, {status: 'searching'}),
        fixtureCall('fired', 'timer_create', 4),
        fixtureCall('board', 'project_board_read', 5),
        fixtureCall('motion', 'motion_video_check', 6, {query: 'Check scene-001'}),
        fixtureCall('memory', 'create_memory', 7),
        fixtureCall('cmd', 'run_command', 8),
      ], {
        read: () => fixtureRow('read_files'),
        todo: round => renderChecklistCard({...round, results: [todo]}, fixtureGlyph, () => '', text),
        watch: round => renderTimerCard({...round, _timerTimerId: 'tmr_fixture_watch', _timerPollInterval: 30,
          _timerConditionKind: 'hybrid', _timerCheckCommand: 'test -f build/ready',
          _timerCheckInstruction: 'Verify the deployment evidence.', _timerNextPollTs: 15000},
          {polls: [{decision: 'wait', pollNum: 0, ts: 1000, pollId: 'p0', reason: 'Not ready yet'}], triggered: false},
          '', timerPorts),
        fired: round => renderTimerCard({...round, _timerTimerId: 'tmr_fixture_done', _timerPollInterval: 30,
          _timerConditionKind: 'code', _timerConditionCommand: 'test -f out'},
          {polls: [{decision: 'ready', pollNum: 1, ts: 1000, pollId: 'p1', reason: 'Ready'}], triggered: true},
          '', timerPorts),
        board: round => renderProjectReceipt({...round, results: [{source: 'Board', boardSnapshot: {open: 1, lanes: {
          open: [{title: 'Plan implementation', owner: 'reviewer'}], claimed: [{title: 'Refactor receipts'}], done: []}}}]},
          fixtureGlyph, '', projectPorts),
        motion: round => renderMotionCard({...round, toolContent: JSON.stringify({ok: false, category: 'layout',
          elapsed: 0, gates: {lint: true, layout: false}, errors: ['Title overlaps chart']})},
          fixtureGlyph, '', {text, scanWorkUnits: () => {}}),
        // The retained memory preview's markup (tool_rounds.js _renderMemoryBlock).
        memory: round => `<details class="ptool-memory-block" data-rn="${round.roundNum}">
          <summary class="ptool-line ptool-memory-header"><span class="ptool-icon">${fixtureGlyph}</span>
            <span class="ptool-text">Save memory: tool panel bodies hang flush</span>
            <span class="ptool-badge ptool-badge-ok">created</span></summary>
          <div class="ptool-memory-body"><div class="ptool-memory-name">Tool panel bodies hang flush</div>
            <div class="ptool-memory-chips"><span class="ptool-memory-chip ptool-memory-chip-scope">project</span>
              <span class="ptool-memory-chip ptool-memory-chip-tag">#frontend</span></div>
            <div class="ptool-memory-desc">Every call body inside an open tool panel hangs on the spine.</div>
            <div class="ptool-memory-content md-content"><p>Rule body paragraph.</p></div></div>
        </details>`,
        cmd: () => fixtureRow('run_command'),
      });
      document.querySelectorAll('.conversation-block[data-turn-zone="process"] details').forEach(node => { node.open = true; });
    }""")
    geometry = frame.evaluate(r"""() => {
      const blockOf = id => document.querySelector(`.conversation-block[data-block-id="tool:${id}"]`);
      const visible = node => Boolean(node) && getComputedStyle(node).display !== 'none'
        && node.getBoundingClientRect().width > 0;
      const timer = id => {
        const host = blockOf(id);
        const sheet = host.querySelector('.timer-watcher-block');
        const header = sheet.querySelector(':scope > .timer-watcher-header');
        const glyph = header.querySelector(':scope > .timer-watcher-icon');
        const label = header.querySelector(':scope > .timer-watcher-label');
        const meta = [...header.querySelectorAll(':scope > :is(.timer-id-chip, .timer-kind-badge)')].map(node => {
          const style = getComputedStyle(node);
          return {...panelRect(node), background: style.backgroundColor, border: style.borderTopWidth,
            color: style.color, font: style.fontFamily};
        });
        const trailing = [...header.children].filter(node => visible(node)
          && !node.matches('.timer-watcher-icon, .timer-watcher-label, .timer-toggle'));
        const body = sheet.querySelector(':scope > .timer-watcher-body');
        return {
          block: panelBlock(host), sheet: panelSheet(sheet),
          glyph: panelRect(glyph), glyphInk: getComputedStyle(glyph).color,
          label: panelRect(label), labelInk: getComputedStyle(label).color,
          labelWeight: getComputedStyle(label).fontWeight, labelFont: getComputedStyle(label).fontFamily,
          meta, toggle: panelRect(header.querySelector(':scope > .timer-toggle')),
          trailingRight: Math.max(...trailing.map(node => node.getBoundingClientRect().right)),
          body: visible(body) ? panelRect(body) : null,
        };
      };
      const body = (id, selector) => {
        const node = blockOf(id).querySelector(selector);
        const style = getComputedStyle(node);
        return {left: node.getBoundingClientRect().left, background: style.backgroundColor,
          spine: [style.borderLeftWidth, style.borderLeftColor], edges: [style.borderTopWidth,
            style.borderRightWidth, style.borderBottomWidth], radius: style.borderTopRightRadius,
          padding: style.paddingLeft};
      };
      const host = blockOf('watch');
      return {
        card: panelCard(), watch: timer('watch'), fired: timer('fired'),
        inks: {intent: panelResolved(host, 'color', 'var(--tool-row-intent-ink)'),
          intentLive: panelResolved(host, 'color', 'var(--tool-row-intent-ink-live)'),
          glyph: panelResolved(host, 'color', 'var(--tool-row-glyph-ink)'),
          glyphLive: panelResolved(host, 'color', 'var(--tool-row-glyph-ink-live)'),
          meta: panelResolved(host, 'color', 'var(--tool-row-meta-ink)'),
          regular: panelResolved(host, 'font-weight', 'var(--weight-regular)'),
          medium: panelResolved(host, 'font-weight', 'var(--weight-medium)')},
        bodies: {todo: body('todo', '.ptool-todo-body'), board: body('board', '.ptool-convmeta-body'),
          motion: body('motion', '.ptool-motion-body'), memory: body('memory', '.ptool-memory-body')},
        overflow: [...document.querySelectorAll('.conversation-block[data-turn-zone="process"]')]
          .some(node => node.scrollWidth > node.clientWidth + 1),
      };
    }""")
    screenshot = tmp_path / f'timer-receipts-panel-{theme}-{width}.png'
    frontend_visual_page.locator('#rich-call-fixture').screenshot(path=str(screenshot))
    print(f'Timer and receipts panel screenshot: {screenshot}')

    card, inks = geometry['card'], geometry['inks']
    assert not geometry['overflow']
    left = geometry['watch']['block']['left']
    text_edge = left + card['glyphColumn'] + card['glyphGap']
    _assert_card_rows(card, text_edge, 8)
    for name, live in [('watch', True), ('fired', False)]:
        watch = geometry[name]
        block = watch['block']
        assert block['radius'] == ['0px'] * 4, 'card sides stay straight through the watch'
        assert watch['sheet'] == NO_SHEET, f'{name}: no sheet inside the card'
        assert watch['glyph']['left'] == pytest.approx(block['left'], abs=0.5)
        assert watch['glyph']['width'] == pytest.approx(card['glyphColumn'], abs=0.5)
        assert watch['label']['left'] == pytest.approx(text_edge, abs=0.5), f'{name}: state label on the intent edge'
        # The copyable id and the condition tier follow the label as quiet metadata.
        assert len(watch['meta']) == 2
        for item in watch['meta']:
            assert item['left'] >= watch['label']['right'] - 0.5
            assert item['top'] < watch['label']['bottom'], f'{name}: metadata stays on the call row'
            assert (item['background'], item['border'], item['color']) == ('rgba(0, 0, 0, 0)', '0px', inks['meta'])
        assert watch['meta'][0]['font'] != watch['labelFont'], 'the id keeps the identity face'
        # The caret closes the row inside the end lane; outcomes end on the rows' outcome column.
        assert watch['toggle']['top'] < watch['label']['bottom'], f'{name}: caret on the call row'
        assert watch['toggle']['right'] == pytest.approx(block['right'] - card['rowEnd'], abs=0.5)
        assert watch['trailingRight'] == pytest.approx(block['right'] - card['endLane'], abs=0.5)
        assert watch['labelInk'] == (inks['intentLive'] if live else inks['intent'])
        assert watch['labelWeight'] == (inks['medium'] if live else inks['regular'])
        assert watch['glyphInk'] == (inks['glyphLive'] if live else inks['glyph'])
    # A live watch opens its checks, hanging from the title to the card's side.
    body = geometry['watch']['body']
    assert body is not None
    assert body['left'] == pytest.approx(text_edge, abs=0.5)
    assert body['right'] == pytest.approx(geometry['watch']['block']['right'], abs=0.5)
    assert geometry['fired']['body'] is None
    # Receipts hang their bodies on the same evidence spine as a checklist.
    bodies = geometry['bodies']
    assert bodies['todo']['background'] == 'rgba(0, 0, 0, 0)'
    assert bodies['board'] == bodies['todo']
    assert bodies['motion'] == bodies['todo']
    assert bodies['memory'] == bodies['todo']
    frame.evaluate('fixtureSurface.dispose()')


@pytest.mark.parametrize('width', [1100, 390])
@pytest.mark.parametrize('theme', ['dark', 'light', 'tofu'])
def test_questions_and_rich_bodies_read_as_calls_of_the_tool_panel(frontend_visual_page, tmp_path, width, theme):
    frame = _mount(frontend_visual_page, theme, width, [
        'frontend/src/conversation/presentation/tool-human-guidance-presentation.ts',
        'frontend/src/conversation/presentation/tool-search-presentation.ts',
    ])
    frame.evaluate(r"""() => {
      const guidance = createToolHumanGuidancePresentation({translate: t, renderMarkdown: escapeHtml});
      const search = createToolSearchPresentation({translate: t, iconHtml: () => ''});
      const choice = {interactionId: 'g-choice', question: 'Which scope should the redesign cover?',
        options: ['Frontend only', 'Frontend + defaults'], descriptions: ['Visual only', 'Also schema'],
        state: 'pending'};
      mountRounds([
        fixtureCall('read', 'read_files', 1),
        fixtureCall('ask', 'ask_human', 2, {status: 'awaiting_human', guidanceId: 'g-free',
          guidanceType: 'free_text', guidanceQuestion: 'Which release should the notes cover?'}),
        fixtureCall('choose', 'ask_human', 3, {status: 'awaiting_human', guidanceId: 'g-choice',
          guidanceType: 'choice', guidanceQuestion: choice.question, guidanceOptions: choice.options}),
        fixtureCall('search', 'web_search', 4, {query: 'q1\nq2',
          results: [{title: 'A', url: 'https://a.com', source: 'google', _q: 'q1'},
            {title: 'B', url: 'https://b.com', source: 'bing', _q: 'q2'}],
          verticals: [{domain: 'academic', sources: [{source: 'hf', identifier: 'x'}],
            items: [{title: 'P1', url: 'https://p1', upvotes: 9}, {title: 'P2', url: 'https://p2', citations: 3}]}]}),
        fixtureCall('cmd', 'run_command', 5),
      ], {
        read: () => fixtureRow('read_files'),
        ask: round => guidance.renderGuidanceHtml(round, {iconHtml: fixtureGlyph}),
        search: round => search.renderSearchHtml(round, round.results,
          {iconHtml: fixtureGlyph, queryHtml: 'q1 · q2', rightControlsHtml: ''}),
        cmd: () => fixtureRow('run_command'),
      }, {
        // The production choice port shape; nothing is submitted here.
        choiceInteractions: () => ({load: async () => choice,
          submit: async () => { throw new Error('not submitted in this fixture'); }, commandId: () => 'command'}),
      });
      const results = document.querySelector('.ptool-results-block');
      results.classList.add('expanded');
      results.querySelector('.ptool-results-header').setAttribute('aria-expanded', 'true');
    }""")
    frame.wait_for_function("""() => document.querySelector('[data-renderer-mode="native-choice"] .rich-block')
      ?.textContent.includes('Frontend only')""")
    geometry = frame.evaluate(r"""() => {
      const blockOf = id => document.querySelector(`.conversation-block[data-block-id="tool:${id}"]`);
      const marker = node => getComputedStyle(node, '::before').content;
      const visible = node => getComputedStyle(node).display !== 'none' && node.getBoundingClientRect().width > 0;
      const ask = blockOf('ask'), choose = blockOf('choose'), search = blockOf('search');
      const card = ask.querySelector('.hg-card');
      const header = card.querySelector(':scope > .hg-header');
      const summary = choose.querySelector(':scope > .conversation-tool-summary');
      const rich = choose.querySelector(':scope > .rich-response');
      const results = search.querySelector('.ptool-results-block');
      const vertical = results.querySelector('.vertical-card');
      // Free-standing sheets outside an open panel keep their owners' frames.
      const loose = ['hg-card', 'rich-block', 'vertical-card'].map(name => {
        const node = document.createElement('div');
        node.className = name;
        document.body.append(node);
        const width = getComputedStyle(node).borderTopWidth;
        node.remove();
        return width;
      });
      return {
        card: panelCard(), loose,
        ask: {block: panelBlock(ask), sheet: panelSheet(card), marker: marker(header),
          title: panelRect(header.querySelector(':scope > .hg-title')),
          badge: panelRect(header.querySelector(':scope > .hg-badge')),
          body: [...card.children].filter(node => node !== header && visible(node)).map(panelRect)},
        choose: {block: panelBlock(choose), marker: marker(summary),
          title: panelRect(summary.querySelector(':scope > .ptool-text')), rich: panelRect(rich),
          sheets: [...rich.querySelectorAll('.rich-block')].map(panelSheet)},
        search: {expanded: results.classList.contains('expanded'), sheet: panelSheet(vertical),
          vertical: panelRect(vertical), content: panelBlock(results.querySelector('.ptool-results-content')),
          heads: [...results.querySelectorAll('.search-query-group-header, .vertical-card-header')].map(node => ({
            background: getComputedStyle(node).backgroundColor,
            text: node.getBoundingClientRect().left + parseFloat(getComputedStyle(node).paddingLeft)})),
          rows: [...results.querySelectorAll('.vertical-row')].map(node =>
            node.getBoundingClientRect().left + parseFloat(getComputedStyle(node).paddingLeft))},
        overflow: [...document.querySelectorAll('.conversation-block[data-turn-zone="process"]')]
          .some(node => node.scrollWidth > node.clientWidth + 1),
      };
    }""")
    screenshot = tmp_path / f'questions-rich-panel-{theme}-{width}.png'
    frontend_visual_page.locator('#rich-call-fixture').screenshot(path=str(screenshot))
    print(f'Questions and rich bodies panel screenshot: {screenshot}')

    card = geometry['card']
    assert not geometry['overflow']
    assert '0px' not in geometry['loose'], 'outside an open panel every family keeps its own sheet'
    ask, choose, search = geometry['ask'], geometry['choose'], geometry['search']
    left, right = ask['block']['left'], ask['block']['right']
    text_edge = left + card['glyphColumn'] + card['glyphGap']
    _assert_card_rows(card, text_edge, 5)
    # A question is one call: no sheet, a `?` marker in the glyph column, the
    # waiting badge on the rows' outcome column, the body from the title's
    # edge to the card's side.
    assert ask['sheet'] == NO_SHEET
    assert ask['marker'].startswith('"?"')
    assert ask['title']['left'] == pytest.approx(text_edge, abs=0.5)
    assert ask['badge']['right'] == pytest.approx(right - card['endLane'], abs=0.5)
    assert ask['body'], 'the question and its answer field show under the row'
    for part in ask['body']:
        assert part['left'] == pytest.approx(text_edge, abs=0.5)
        assert part['right'] == pytest.approx(right, abs=0.5)
    # A native choice form hangs the same way under the summary row.
    assert choose['marker'].startswith('"?"')
    assert choose['title']['left'] == pytest.approx(text_edge, abs=0.5)
    assert choose['rich']['left'] == pytest.approx(text_edge, abs=0.5)
    assert choose['rich']['right'] == pytest.approx(choose['block']['right'], abs=0.5)
    assert choose['sheets'], 'the choice form rendered'
    for sheet in choose['sheets']:
        assert (sheet['border'], sheet['background'], sheet['radius']) == (['0px'] * 4, 'rgba(0, 0, 0, 0)', '0px')
    # A vertical-source group spans the open results like the query groups.
    assert search['expanded']
    assert search['sheet'] == NO_SHEET
    assert search['vertical']['left'] == pytest.approx(search['content']['left'], abs=0.5)
    assert search['vertical']['right'] == pytest.approx(search['content']['right'], abs=0.5)
    # Its group heads (two queries, one vertical source) paint no band and
    # start, like the source rows, on the results' edge.
    assert len(search['heads']) == 3
    for head in search['heads']:
        assert head['background'] == 'rgba(0, 0, 0, 0)'
        assert head['text'] == pytest.approx(search['content']['left'], abs=0.5)
    assert len(search['rows']) == 2
    assert search['rows'] == [pytest.approx(search['content']['left'], abs=0.5)] * 2
    frame.evaluate('fixtureSurface.dispose()')


@pytest.mark.parametrize('width', [1100, 390])
@pytest.mark.parametrize('theme', ['dark', 'light', 'tofu'])
def test_reports_and_receipt_lists_read_as_lines_of_their_call(frontend_visual_page, tmp_path, width, theme):
    frame = _mount(frontend_visual_page, theme, width, [
        'frontend/src/conversation/ui/project-receipt/index.ts',
        'frontend/src/conversation/presentation/tool-injection-presentation.ts',
    ])
    frame.evaluate(r"""() => {
      const locale = fixtureLocale;
      const text = (key, fallback) => typeof fallback === 'object' && fallback
        ? (locale[key] || key).replace('{n}', fallback.n) : locale[key] || fallback || key;
      const icon = (name, size = 14) => fixtureGlyph.replaceAll('"14"', `"${size}"`);
      const projectPorts = {text, markdown: () => '', now: () => 1700001000000,
        absoluteTime: value => new Date(value).toISOString(), conversationTitle: () => 'Review implementation', icon};
      const injection = createToolInjectionPresentation({translate: t, renderMarkdown: source => `<p>${escapeHtml(source)}</p>`,
        iconHtml: icon, resolveConversationTitle: () => 'Review implementation'});
      const update = (id, role, status, body) => `<swarm-update><agent-id>${id}</agent-id><role>${role}</role>`
        + `<status>${status}</status><elapsed-seconds>42</elapsed-seconds><tokens>3200</tokens>${body}</swarm-update>`;
      // Each sidecar receipt as the retained bridge rehydrates it into a round.
      const injected = {
        'inject:inbox': {_inboxInject: true, inboxCount: 2, inboxAgentIds: ['a1', 'a2'], inboxPreviews: [
          {agentId: 'a1', text: update('a1', 'coder', 'completed',
            '<result>Found three call sites.</result><output-file>logs/a1.md</output-file>')},
          {agentId: 'a2', text: update('a2', 'reviewer', 'failed', '<error>Provider rejected the model.</error>')}]},
        'inject:steer': {_userSteerInject: true, steerCount: 1, steerPreviews: [{text: 'Please also check the dark theme.'}]},
        'inject:command': {_bgCommandInject: true, bgCommandCount: 1,
          bgCommandPreviews: [{commandId: 'bg_1a2b', text: '12 passed in 3.4s'}]},
      };
      const receipt = (blockId, round) => ({blockId, round, count: 1, previews: []});
      mountRounds([
        fixtureCall('read', 'read_files', 1),
        fixtureCall('board', 'project_board_read', 2),
        fixtureCall('commit', 'project_commit', 3),
        fixtureCall('cmd', 'run_command', 4),
      ], {
        read: () => fixtureRow('read_files'),
        board: round => renderProjectReceipt({...round, results: [{source: 'Board', boardSnapshot: {open: 1, lanes: {
          open: [{title: 'Plan implementation', owner: 'reviewer'}], claimed: [{title: 'Refactor receipts'}], done: []}}}]},
          fixtureGlyph, '', projectPorts),
        commit: round => renderProjectReceipt({...round, results: [{source: 'Board', commitResult: {ok: true,
          verified: true, commitSha: 'abc123def4567', committed: ['frontend/src/a.ts', 'frontend/src/styles/b.css'],
          excluded: [{path: 'docs/c.md', numstat: '+3 -1', reason: 'a sibling conversation also edited it'}]}}]},
          fixtureGlyph, '', projectPorts),
        cmd: () => fixtureRow('run_command'),
      }, {
        renderInjectionBlockHtml: block => injection.renderInjectionHtml({
          ...injected[block.items[0].blockId], roundNum: block.items[0].round}),
      }, {
        _inboxInjects: [receipt('inject:inbox', 2)],
        _userSteerInjects: [receipt('inject:steer', 3)],
        _bgCommandInjects: [receipt('inject:command', 4)],
      });
      document.querySelectorAll('.conversation-block[data-turn-zone="process"] details').forEach(node => { node.open = true; });
    }""")
    geometry = frame.evaluate(r"""() => {
      const contentEdges = node => {
        const style = getComputedStyle(node);
        const box = node.getBoundingClientRect();
        return {left: box.left + parseFloat(style.borderLeftWidth) + parseFloat(style.paddingLeft),
          right: box.right - parseFloat(style.borderRightWidth) - parseFloat(style.paddingRight)};
      };
      const line = node => {
        const style = getComputedStyle(node);
        const box = node.getBoundingClientRect();
        return {left: box.left, right: box.right, background: style.backgroundColor, radius: style.borderTopLeftRadius,
          border: [style.borderTopWidth, style.borderRightWidth, style.borderBottomWidth, style.borderLeftWidth],
          follows: Boolean(node.previousElementSibling?.matches('.sw-card')),
          body: contentEdges(node.closest('.sw-inbox-row-body, .ptool-convmeta-body'))};
      };
      // Free-standing items outside an open panel keep their owners' frames.
      const loose = ['sw-card', 'ptool-board-mini-card', 'ptool-commit-file'].map(name => {
        const node = document.createElement('div');
        node.className = name;
        document.body.append(node);
        const width = getComputedStyle(node).borderTopWidth;
        node.remove();
        return width;
      });
      return {
        card: panelCard(), loose,
        hairline: panelResolved(document.body, 'padding-top', 'var(--control-edge-inset)'),
        injections: document.querySelectorAll('.conversation-block--injections[data-turn-zone="process"]').length,
        reports: [...document.querySelectorAll('.sw-card')].map(line),
        lists: [...document.querySelectorAll('.ptool-board-mini-card, .ptool-commit-file')].map(line),
        surfaces: [...document.querySelectorAll('.sw-card-error, .sw-card-raw-pre')]
          .map(node => getComputedStyle(node).backgroundColor),
        lastParagraphs: [...document.querySelectorAll('.sw-card-preview')]
          .map(node => getComputedStyle(node.lastElementChild).marginBottom),
        overflow: [...document.querySelectorAll('.conversation-block[data-turn-zone="process"]')]
          .some(node => node.scrollWidth > node.clientWidth + 1),
      };
    }""")
    screenshot = tmp_path / f'reports-lists-panel-{theme}-{width}.png'
    frontend_visual_page.locator('#rich-call-fixture').screenshot(path=str(screenshot))
    print(f'Reports and receipt lists panel screenshot: {screenshot}')

    card = geometry['card']
    assert not geometry['overflow']
    assert '0px' not in geometry['loose'], 'outside an open panel every item keeps its own frame'
    assert geometry['injections'] == 3, 'injection receipts ride the open panel'
    block = frame.evaluate("""() => panelBlock(document.querySelector('.conversation-block[data-block-id="tool:read"]'))""")
    _assert_card_rows(card, block['left'] + card['glyphColumn'] + card['glyphGap'], 7)
    # Reports are lines of their receipt: no frame, fill, stripe or radius,
    # from the body's edge to its side; consecutive reports part on a hairline.
    assert len(geometry['reports']) == 4
    for report in geometry['reports']:
        top = geometry['hairline'] if report['follows'] else '0px'
        assert (report['background'], report['radius']) == ('rgba(0, 0, 0, 0)', '0px')
        assert report['border'] == [top, '0px', '0px', '0px']
        assert report['left'] == pytest.approx(report['body']['left'], abs=0.5)
        assert report['right'] == pytest.approx(report['body']['right'], abs=0.5)
    assert sum(report['follows'] for report in geometry['reports']) == 1
    # Board cards and commit files are plain lines under their group heads.
    assert len(geometry['lists']) == 5
    for item in geometry['lists']:
        assert (item['background'], item['radius'], item['border']) == ('rgba(0, 0, 0, 0)', '0px', ['0px'] * 4)
        assert item['left'] == pytest.approx(item['body']['left'], abs=0.5)
        assert item['right'] == pytest.approx(item['body']['right'], abs=0.5)
    # An error line and raw output keep their surfaces; a report's last
    # paragraph leaves no blank line before the next call.
    assert len(geometry['surfaces']) == 2
    assert 'rgba(0, 0, 0, 0)' not in geometry['surfaces']
    assert geometry['lastParagraphs'] == ['0px', '0px']
    frame.evaluate('fixtureSurface.dispose()')
