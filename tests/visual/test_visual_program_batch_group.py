"""Real Chromium: a model round's sibling programs fold under one batch head.

The synthetic Surface fixture has the reported shape: one response issued nine
``execute_program`` calls, each reading one document; one program completed and
eight returned for model judgment after their reads were refused. A live Turn
runs the same round one program at a time, and a later round has a lone
program. Production Surface, renderers and shipped CSS run on a local HTTP
origin; no storage, provider or gateway traffic is involved.
"""
import json

import pytest

from tests._runtime_sections import ROOT, native_module_path
from tests.support.frontend_visual import (
    frontend_visual_origin as frontend_visual_origin,
    frontend_visual_page as frontend_visual_page,
)

pytestmark = pytest.mark.visual

IDS = [f'doc-{n}' for n in range(1, 10)]
PROGRAMS = {f'program:{id_}' for id_ in IDS}
CHILDREN = {f'tool:child-{id_}' for id_ in IDS} | {'tool:write-doc-1'}


def _label(messages, *parts):
    return ' · '.join(messages[key].replace('{n}', str(count)) for key, count in parts)


@pytest.mark.parametrize('language', ['zh', 'en'])
@pytest.mark.parametrize('width', [1280, 390], ids=['desktop', 'mobile'])
def test_sibling_programs_fold_under_one_counted_head(frontend_visual_page, tmp_path, width, language):
    page = frontend_visual_page
    page.set_viewport_size({'width': width, 'height': 1000})
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
        '.native/program-batch-group-visual.js', ROOT / 'frontend/src/conversation/index.ts'))
    page.evaluate(r"""messages => {
      const text = (key, fallback, values) => String(messages[key] ?? fallback)
        .replace(/\{(\w+)\}/g, (token, name) => values && Object.hasOwn(values, name) ? String(values[name]) : token);
      const ids = Array.from({length: 9}, (_, index) => `doc-${index + 1}`);
      const code = id => `const r = await tools.invoke('mcp__xuecheng__read_doc', {doc: '${id}'});\n`
        + `return {id: '${id}', head: String(r).slice(0, 150)};`;
      const gateway = (scope, id, llmRound, status) => ({
        ...scope, type:'tool_use', blockId:'tool:' + id, id, name:'execute_program', llmRound,
        input:{code:code(id)},
        _round:{...scope, toolCallId:id, toolName:'execute_program', llmRound, status,
          attentionKind:'routine', toolArgs:{code:code(id)}},
      });
      const child = (scope, parent, llmRound, status, toolName = 'mcp__xuecheng__read_doc') => {
        const id = (toolName === 'write_file' ? 'write-' : 'child-') + parent;
        return {...scope, type:'tool_use', blockId:'tool:' + id, id, name:toolName, llmRound,
          input:{doc:parent},
          _round:{...scope, toolCallId:id, toolName, llmRound, status, parentToolCallId:parent,
            attentionKind:toolName === 'write_file' ? 'important' : 'routine', toolArgs:{doc:parent},
            results:[{text:status === 'error' ? 'MCP Error: synthetic permission denied (619)'
              : 'Synthetic document'}]}};
      };
      const program = (scope, id, llmRound, roundNum, fields) => ({
        ...scope, roundNum, llmRound, _programSynthetic:true, _programCallId:id,
        programSource:'execute_program', programBackend:'local_javascript', programCode:code(id),
        childCallIds:['child-' + id], childToolNames:['mcp__xuecheng__read_doc'], ...fields,
      });
      const completed = {programStatus:'completed', status:'done',
        childCallIds:['child-doc-1', 'write-doc-1'], childToolNames:['mcp__xuecheng__read_doc', 'write_file'],
        programStats:{outcome:'completed', tool_calls:2, successful_tool_calls:2},
        programResult:{id:'doc-1', head:'Synthetic document'}};
      // A refused read is returned to the model for judgment, not a failure.
      const handedBack = {programStatus:'completed', status:'done',
        programStats:{outcome:'needs_model', tool_calls:1, successful_tool_calls:0},
        programResult:{kind:'observation', reason:'tool_not_done',
          data:{status:'error', name:'mcp__xuecheng__read_doc', error:'Synthetic permission denied (619)'}}};
      const running = {programStatus:'running', status:'searching'};
      const failed = {programStatus:'error', status:'error',
        programResult:{code:'tool_failed', message:'Synthetic program failure'}};
      const settledScope = {taskId:'settled-task', attemptId:'settled-attempt'};
      const liveScope = {taskId:'live-task', attemptId:'live-attempt'};
      const settled = {
        turnId:'settled-turn', conversationId:'program-batch', laneId:'main', ordinal:1,
        actor:'assistant', kind:'reply', status:'completed', currentAttemptId:null,
        projectionRevision:1, createdAt:1, updatedAt:1, settlement:{outcome:'completed'},
        projection:{
          segments:[
            {...settledScope, type:'text', blockId:'narration', llmRound:36, text:'Reading the nine documents.'},
            ...ids.map(id => gateway(settledScope, id, 36, 'done')),
            child(settledScope, 'doc-1', 36, 'done'), child(settledScope, 'doc-1', 36, 'done', 'write_file'),
            ...ids.slice(1).map(id => child(settledScope, id, 36, id === 'doc-2' ? 'done' : 'error')),
            gateway(settledScope, 'solo', 37, 'done'),
            {...settledScope, type:'text', blockId:'answer', llmRound:38, deliverable:true, terminal:true,
              text:'One document was read; eight reads were refused.'},
          ],
          toolRounds:[
            ...ids.map((id, index) => program(settledScope, id, 36, 36000001 + index,
              index ? handedBack : completed)),
            program(settledScope, 'solo', 37, 37000001, handedBack),
          ],
        },
      };
      // `fields` maps each started program to its state; the rest are queued calls.
      const live = (status, fields) => ({
        turnId:'live-turn', conversationId:'program-batch', laneId:'main', ordinal:2,
        actor:'assistant', kind:'reply', status, currentAttemptId:status === 'running' ? liveScope.attemptId : null,
        projectionRevision:1, createdAt:2, updatedAt:2,
        ...(status === 'running' ? {} : {settlement:{outcome:'completed'}}),
        projection:{
          segments:[
            ...ids.map(id => gateway(liveScope, id, 5, fields[id]?.status === 'done' ? 'done'
              : fields[id]?.status === 'error' ? 'error' : 'searching')),
            ...ids.filter(id => fields[id]?.programStatus === 'completed')
              .map(id => child(liveScope, id, 5, 'error')),
          ],
          toolRounds:ids.filter(id => fields[id])
            .map(id => program(liveScope, id, 5, 5000001 + ids.indexOf(id), fields[id])),
        },
      });
      const surface = createConversationSurface(document.getElementById('surface'), {
        ...createClassicConversationRenderers({
          renderSafeMarkdownHtml(value) {
            const paragraph = document.createElement('p');
            paragraph.textContent = value;
            return paragraph.outerHTML;
          },
          localizedText: text,
        }),
        roundOutlineLabel: (key, n) => text(key, key, {n}),
      });
      window.paintLive = (status, fields) => {
        const turns = [settled, live(status, fields)];
        surface.render(selectConversationViewModel({...createTurnState('program-batch'),
          turnsById:Object.fromEntries(turns.map(turn => [turn.turnId, turn])),
          laneOrder:{main:turns.map(turn => turn.turnId)}}));
      };
      window.fixtureStates = {completed, handedBack, running, failed};
      paintLive('running', {'doc-1':running});
      window.snapshot = () => {
        const lines = node => {
          const range = document.createRange();
          range.selectNodeContents(node);
          return new Set([...range.getClientRects()].map(rect => Math.round(rect.top))).size;
        };
        const shown = node => Boolean(node) && node.getClientRects().length > 0
          && getComputedStyle(node).visibility !== 'hidden';
        const turn = turnId => {
          const root = document.querySelector(`[data-turn-id="${turnId}"]`);
          const blocks = [...root.querySelectorAll('.conversation-block[data-block-id]')];
          return {
            heads: [...root.querySelectorAll('.conversation-program-batch')].map(head => {
              const label = head.querySelector('.conversation-program-batch__label');
              const failedCount = label.querySelector('[data-program-status="failed"]');
              const box = head.getBoundingClientRect();
              return {
                label: label.textContent,
                expanded: head.getAttribute('aria-expanded'),
                attention: head.dataset.attention,
                labelLines: lines(label),
                left: box.left, right: box.right,
                next: head.nextElementSibling?.dataset.blockId ?? null,
                // The head travels with its first member; the layout alone places prose.
                narrationOrder: (() => {
                  const narration = root.querySelector('[data-block-id="narration"]');
                  return narration ? narration.compareDocumentPosition(head)
                    === narration.compareDocumentPosition(head.nextElementSibling) : null;
                })(),
                failedColored: failedCount ? getComputedStyle(failedCount).color
                  !== getComputedStyle(label.firstElementChild).color : null,
              };
            }),
            blocks: blocks.map(node => {
              const card = node.querySelector('details.ptc-card');
              const head = card?.querySelector(':scope > .ptc-head');
              const visible = head ? [...head.children].filter(shown)
                .map(child => child.getBoundingClientRect()) : [];
              return {
                id: node.dataset.blockId,
                batch: node.dataset.programBatch ?? null,
                shown: shown(node),
                open: card ? card.open : node.querySelector('details')?.open ?? null,
                meta: head?.querySelector('.ptc-meta')?.textContent ?? '',
                headerLines: new Set(visible.map(box => Math.round(box.top + box.height / 2))).size,
                right: card ? card.getBoundingClientRect().right : null,
              };
            }),
          };
        };
        return {
          viewport: document.documentElement.clientWidth,
          scrollWidth: document.documentElement.scrollWidth,
          settled: turn('settled-turn'),
          live: turn('live-turn'),
        };
      };
    }""", messages)

    evidence = tmp_path / f'program-batch-{width}-{language}'
    shots = []

    def capture(stage):
        snapshot = page.evaluate('snapshot()')
        path = evidence.with_name(f'{evidence.name}-{stage}')
        path.with_suffix('.json').write_text(json.dumps(snapshot, indent=2) + '\n')
        page.screenshot(path=str(path.with_suffix('.png')), full_page=True)
        shots.append(str(path.with_suffix('.png')))
        assert snapshot['scrollWidth'] <= snapshot['viewport'] + 1, stage
        for name in ('settled', 'live'):
            for head in snapshot[name]['heads']:
                assert head['right'] <= width + 1 and head['left'] >= 0, (stage, head)
                # One line on desktop; a narrow pane may wrap the counts once.
                assert head['labelLines'] <= (2 if width < 600 else 1), (stage, head)
        return snapshot

    def blocks(snapshot, name):
        return {block['id']: block for block in snapshot[name]['blocks']}

    settled_label = _label(messages, ('roundOutline.programs', 9), ('roundOutline.programsCompleted', 1),
                           ('roundOutline.programsHandoff', 8))
    # Settled: the reader opens the Turn's tool panel; one collapsed line counts
    # the round and nothing of it is shown.
    page.locator('[data-turn-id="settled-turn"] .conversation-tool-panel__toggle').click()
    first = capture('collapsed')
    print(f'Program batch browser evidence: {evidence}')
    [head] = first['settled']['heads']
    assert head['label'] == settled_label
    assert (head['expanded'], head['attention'], head['next']) == ('false', 'important', 'program:doc-1')
    assert head['narrationOrder'] is True
    settled = blocks(first, 'settled')
    batched = {id_ for id_, block in settled.items() if block['batch']}
    assert batched == PROGRAMS | CHILDREN
    assert not any(settled[id_]['shown'] for id_ in batched)
    assert settled['answer']['shown']
    # The lone program of the next round keeps today's card: no head, its handoff opens.
    assert settled['program:solo']['batch'] is None
    assert settled['program:solo']['shown'] and settled['program:solo']['open'] is True
    [live_head] = first['live']['heads']
    assert live_head['label'] == _label(messages, ('roundOutline.programs', 9),
                                        ('roundOutline.programsRunning', 1), ('roundOutline.programsWaiting', 8))
    assert (live_head['expanded'], live_head['attention']) == ('false', 'active')

    # Expanded: compact closed cards, in authored order, beside their child rows.
    page.locator('[data-turn-id="settled-turn"] .conversation-program-batch').click()
    second = capture('expanded')
    assert second['settled']['heads'][0]['expanded'] == 'true'
    settled = blocks(second, 'settled')
    assert all(settled[id_]['shown'] for id_ in PROGRAMS | CHILDREN)
    # A child's refused read is its program's evidence: a closed row.
    assert not any(settled[id_]['open'] for id_ in CHILDREN), {id_: settled[id_] for id_ in CHILDREN}
    for id_ in PROGRAMS:
        card = settled[id_]
        assert card['open'] is False, id_
        # A narrow pane may move a long status beneath the title (RENDER_CONTRACT).
        assert card['headerLines'] <= (2 if card['meta'] else 1) + (width < 600), (id_, card)
        assert card['right'] <= width + 1, id_
    assert [block['id'] for block in second['settled']['blocks'] if block['id'] in PROGRAMS] == \
        [f'program:{id_}' for id_ in IDS]
    assert settled['program:solo']['open'] is True

    # Live: the reader's choice holds while counts change and through settlement.
    live_head_node = page.locator('[data-turn-id="live-turn"] .conversation-program-batch')
    live_head_node.focus()
    live_head_node.press('Enter')
    page.evaluate("""() => {
      window.liveHead = document.querySelector('[data-turn-id="live-turn"] .conversation-program-batch');
      const {handedBack, running} = fixtureStates;
      paintLive('running', {'doc-1': handedBack, 'doc-2': running});
    }""")
    third = capture('live')
    [live_head] = third['live']['heads']
    assert live_head['label'] == _label(messages, ('roundOutline.programs', 9), ('roundOutline.programsRunning', 1),
                                        ('roundOutline.programsWaiting', 7), ('roundOutline.programsHandoff', 1))
    assert live_head['expanded'] == 'true'
    assert page.evaluate("liveHead === document.querySelector('[data-turn-id=\"live-turn\"] .conversation-program-batch')")
    assert third['settled']['heads'][0]['expanded'] == 'true'
    live = blocks(third, 'live')
    assert all(block['open'] is False for block in live.values() if block['open'] is not None), live

    page.evaluate("""() => {
      const {completed, handedBack, failed} = fixtureStates;
      paintLive('completed', Object.fromEntries(Array.from({length: 9}, (_, index) => [`doc-${index + 1}`,
        index === 0 ? completed : index === 1 ? failed : handedBack])));
    }""")
    fourth = capture('live-settled')
    [live_head] = fourth['live']['heads']
    assert live_head['label'] == _label(messages, ('roundOutline.programs', 9), ('roundOutline.programsCompleted', 1),
                                        ('roundOutline.programsHandoff', 7), ('roundOutline.programsFailed', 1))
    assert (live_head['expanded'], live_head['attention'], live_head['failedColored']) == ('true', 'error', True)
    live = blocks(fourth, 'live')
    # A genuine failure reveals itself as direct-batch failures do; handoffs stay closed.
    assert {id_ for id_, block in live.items() if block['open']} == {'program:doc-2'}

    page.locator('[data-turn-id="settled-turn"] .conversation-program-batch').click()
    fifth = page.evaluate('snapshot()')
    assert fifth['settled']['heads'][0]['expanded'] == 'false'
    assert not any(block['shown'] for block in fifth['settled']['blocks'] if block['batch'])
    print('Program batch screenshots: ' + ', '.join(shots))
