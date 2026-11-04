"""Real Chromium: program rows state their lifecycle in one compact header.

The synthetic Surface fixture has the reported shape: a live Turn whose sibling
``execute_program`` calls wait for one started program, beside a settled Turn
with returned, failed and never-answered calls. Production Surface, renderer
and shipped CSS run on a local HTTP origin; no storage, provider or gateway
traffic is involved.
"""
import json

import pytest

from tests._runtime_sections import ROOT, native_module_path
from tests.support.frontend_visual import (
    frontend_visual_origin as frontend_visual_origin,
    frontend_visual_page as frontend_visual_page,
)

pytestmark = pytest.mark.visual

WAITING = ('tool:queued-1', 'tool:queued-2', 'tool:announced')


@pytest.mark.parametrize('language', ['zh', 'en'])
@pytest.mark.parametrize('width', [1280, 390], ids=['desktop', 'mobile'])
def test_program_rows_name_waiting_running_and_settled_calls_on_one_header(
        frontend_visual_page, tmp_path, width, language):
    page = frontend_visual_page
    page.set_viewport_size({'width': width, 'height': 1800})
    messages = json.loads((ROOT / 'frontend/src/i18n/locales' / f'{language}.json').read_text())
    page.evaluate("""language => {
      document.documentElement.dataset.theme = 'light';
      document.documentElement.lang = language;
      document.body.innerHTML = '<main id="surface"></main>';
    }""", language)
    page.add_style_tag(path=str(ROOT / 'frontend/src/conversation/ui/conversation-surface.css'))
    page.add_style_tag(content='body{display:block;margin:0;padding:var(--space-4);'
                              'height:auto;overflow:auto}#surface{display:block;max-width:100%}')
    page.add_script_tag(path=native_module_path(
        '.native/program-row-status-visual.js', ROOT / 'frontend/src/conversation/index.ts'))
    page.evaluate(r"""messages => {
      const text = (key, fallback, values) => String(messages[key] ?? fallback)
        .replace(/\{(\w+)\}/g, (token, name) => values && Object.hasOwn(values, name) ? String(values[name]) : token);
      const code = id => `const doc = await tools.invoke('mcp__xuecheng__read_doc', {doc: '${id}'}); return doc;`;
      const gateway = (scope, id, llmRound, status, fields) => ({
        ...scope, type:'tool_use', blockId:'tool:' + id, id, name:'execute_program', llmRound,
        input:{code:code(id)},
        // A null status is a call announced before its round exists.
        ...(status === null ? {} : {_round:{...scope, toolCallId:id, toolName:'execute_program',
          llmRound, status, toolArgs:{code:code(id)}, ...fields}}),
      });
      const program = (scope, id, llmRound, roundNum, fields) => ({
        ...scope, roundNum, llmRound, _programSynthetic:true, _programCallId:id,
        programSource:'execute_program', programBackend:'local_javascript',
        programCode:code(id), childCallIds:[], childToolNames:[], ...fields,
      });
      const settled = {taskId:'settled-task', attemptId:'settled-attempt'};
      const live = {taskId:'live-task', attemptId:'live-attempt'};
      const turns = [{
        turnId:'settled-turn', conversationId:'program-row-status', laneId:'main', ordinal:1,
        actor:'assistant', kind:'reply', status:'completed', currentAttemptId:null,
        projectionRevision:1, createdAt:1, updatedAt:1, settlement:{outcome:'completed'},
        projection:{
          segments:[gateway(settled, 'returned', 5, 'done'), gateway(settled, 'failed', 5, 'error'),
            gateway(settled, 'unanswered', 5, 'searching'),
            // An unowned gateway failure keeps its attention row.
            gateway(settled, 'refused', 5, 'error', {toolContent:JSON.stringify(
              {error:{message:'Synthetic permission denied (619)'}})})],
          toolRounds:[
            program(settled, 'returned', 5, 5000001, {programStatus:'completed', status:'done',
              programResult:{title:'Synthetic document'}, childCallIds:['read-1'],
              childToolNames:['mcp__xuecheng__read_doc'],
              programStats:{tool_calls:1, successful_tool_calls:1}}),
            program(settled, 'failed', 5, 5000002, {programStatus:'error', status:'error',
              programResult:{code:'tool_failed', message:'Synthetic permission denied'}}),
          ],
        },
      }, {
        turnId:'live-turn', conversationId:'program-row-status', laneId:'main', ordinal:2,
        actor:'assistant', kind:'reply', status:'running', currentAttemptId:live.attemptId,
        projectionRevision:1, createdAt:2, updatedAt:2,
        projection:{
          segments:[gateway(live, 'started', 36, 'searching'), gateway(live, 'queued-1', 36, 'searching'),
            gateway(live, 'queued-2', 36, 'searching'), gateway(live, 'announced', 36, null)],
          toolRounds:[program(live, 'started', 36, 36000001, {programStatus:'running', status:'searching'})],
        },
      }];
      const state = {...createTurnState('program-row-status'),
        turnsById:Object.fromEntries(turns.map(turn => [turn.turnId, turn])),
        laneOrder:{main:turns.map(turn => turn.turnId)}};
      const surface = createConversationSurface(document.getElementById('surface'),
        createClassicConversationRenderers({
          renderSafeMarkdownHtml(value) {
            const paragraph = document.createElement('p');
            paragraph.textContent = value;
            return paragraph.outerHTML;
          },
          localizedText: text,
        }));
      surface.render(selectConversationViewModel(state));
    }""", messages)
    # Each Turn's sibling programs fold under one batch head
    # (docs/RENDER_CONTRACT.md); the reader expands both to read every row.
    heads = page.locator('.conversation-program-batch')
    assert heads.count() == 2
    for index in range(2):
        head = heads.nth(index)
        assert head.get_attribute('aria-expanded') == 'false'
        head.click()
        assert head.get_attribute('aria-expanded') == 'true'

    snapshot = page.evaluate("""() => {
      const lines = node => {
        const range = document.createRange();
        range.selectNodeContents(node);
        return new Set([...range.getClientRects()].map(rect => Math.round(rect.top))).size;
      };
      const shown = node => Boolean(node) && node.getClientRects().length > 0
        && getComputedStyle(node).visibility !== 'hidden';
      return {
        viewport: document.documentElement.clientWidth,
        scrollWidth: document.documentElement.scrollWidth,
        cards: [...document.querySelectorAll('.ptc-card')].map(card => {
          const head = card.querySelector(':scope > .ptc-head');
          const title = head.querySelector('.ptc-title');
          const status = head.querySelector('.ptc-status');
          const chevron = head.querySelector('.ptc-chevron');
          const middle = node => {
            const box = node.getBoundingClientRect();
            return box.top + box.height / 2;
          };
          const firstLine = [title, status, chevron].map(middle);
          const visible = [...head.children].filter(shown).map(node => node.getBoundingClientRect());
          return {
            block: card.closest('.conversation-block')?.dataset.blockId,
            state: card.closest('.conversation-block')?.dataset.actionState,
            title: title.textContent,
            status: status.textContent,
            meta: head.querySelector('.ptc-meta')?.textContent ?? '',
            titleLines: lines(title),
            titleClipped: title.scrollWidth > title.clientWidth + 1
              || title.scrollHeight > title.clientHeight + 1,
            statusLines: lines(status),
            identityShown: shown(head.querySelector('.conversation-tool-identity')),
            firstLineSpread: Math.max(...firstLine) - Math.min(...firstLine),
            headerLines: new Set(visible.map(box => Math.round(box.top + box.height / 2))).size,
            right: card.getBoundingClientRect().right,
          };
        }),
      };
    }""")
    evidence = tmp_path / f'program-row-status-{width}-{language}'
    evidence.with_suffix('.json').write_text(json.dumps(snapshot, indent=2) + '\n')
    page.screenshot(path=str(evidence.with_suffix('.png')), full_page=True)
    print(f'Program row status browser evidence: {evidence}')

    cards = {card['block']: card for card in snapshot['cards']}
    assert set(cards) == {'program:returned', 'program:failed', 'tool:unanswered', 'tool:refused',
                          'program:started', *WAITING}
    assert cards['tool:refused']['status'] == messages['ptc.status.failed']
    assert cards['tool:refused']['state'] == 'attention'
    assert 'Synthetic permission denied (619)' in cards['tool:refused']['meta']
    for block in WAITING:
        assert cards[block]['status'] == messages['ptc.status.waiting'], block
        # A queued program has requested nothing yet: its header is one line.
        assert cards[block]['meta'] == '', block
        assert cards[block]['headerLines'] == 1, block
    assert cards['program:started']['status'] == messages['ptc.status.running']
    assert cards['tool:unanswered']['status'] == messages['ptc.status.incomplete']
    assert messages['ptc.status.running'] not in {
        card['status'] for block, card in cards.items() if block != 'program:started'}
    for block, card in cards.items():
        assert card['title'] == messages['ptc.title'], block
        assert card['titleLines'] == 1 and not card['titleClipped'], (block, card)
        assert card['statusLines'] == 1, (block, card)
        # The localized title names the program; execute_program stays in its tooltip.
        assert not card['identityShown'], block
        assert card['right'] <= width + 1, block
        # Title, status and caret share one compact line; a summary takes its own.
        assert card['firstLineSpread'] < 4, (block, card)
        assert card['headerLines'] == (2 if card['meta'] else 1), (block, card)
    assert snapshot['scrollWidth'] <= snapshot['viewport'] + 1
