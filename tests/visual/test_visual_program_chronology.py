"""Childless program chronology through the real selector, classic renderer and Surface.

The local HTTP graph fixture deliberately avoids storage and provider traffic;
application journeys remain a separate gate. All transcript data is synthetic.
"""
import json

import pytest

from tests._runtime_sections import ROOT, native_module_path
from tests.support.frontend_visual import (  # noqa: F401
    frontend_visual_origin,
    frontend_visual_page,
)

pytestmark = pytest.mark.visual


@pytest.mark.parametrize('width', [1280, 390], ids=['desktop', 'mobile'])
def test_childless_program_keeps_authored_chronology_and_reader_disclosures(
        frontend_visual_page, tmp_path, width):
    page = frontend_visual_page
    page.set_viewport_size({'width': width, 'height': 1000})
    page.evaluate("""() => {
      document.documentElement.dataset.theme = 'light';
      document.documentElement.lang = 'en';
      document.body.innerHTML = '<main id="surface"></main>';
    }""")
    # native_module_path strips CSS imports. Use the authored Surface stylesheet
    # plus shipped application CSS from the fixture; only the shell is synthetic.
    page.add_style_tag(path=str(ROOT / 'frontend/src/conversation/ui/conversation-surface.css'))
    page.add_style_tag(content='body{display:block;margin:0;padding:var(--space-4);'
                              'height:auto;overflow:auto}#surface{display:block;max-width:100%}')
    page.add_script_tag(path=native_module_path(
        '.native/program-chronology-visual.js', ROOT / 'frontend/src/conversation/index.ts'))
    page.evaluate(r"""() => {
      const scope = {taskId:'synthetic-task', attemptId:'synthetic-attempt'};
      const program = {
        ...scope, roundNum:11000000, llmRound:11, _programSynthetic:true,
        _programCallId:'gateway-11', programSource:'execute_program',
        programBackend:'javascript', programStatus:'running', status:'searching',
        programCode:'return {round: 11, synthetic: true};',
        childCallIds:[], childToolNames:[],
      };
      const gateway = {
        ...scope, type:'tool_use', blockId:'tool:gateway-11', id:'gateway-11',
        name:'execute_program', llmRound:11, input:{code:program.programCode},
        _round:{...scope, toolCallId:'gateway-11', toolName:'execute_program',
          llmRound:11, status:'searching', toolArgs:{code:program.programCode}},
      };
      const thinking = (round, text) => ({...scope, type:'thinking',
        blockId:'thinking-'+round, llmRound:round, text});
      const readRound = {
        ...scope, roundNum:12, llmRound:12, toolCallId:'read-12',
        toolName:'read_files', status:'done', toolArgs:{path:'synthetic/round-twelve.txt'},
        results:[{text:'Synthetic round twelve evidence.', exitCode:0}],
      };
      const laterSegments = [thinking(12, 'Round twelve'),
        {...scope, type:'text', blockId:'narration-12', llmRound:12,
          text:'Reading the synthetic round twelve evidence.'},
        {...scope, type:'tool_use', blockId:'tool:read-12', id:'read-12',
          name:'read_files', llmRound:12, input:readRound.toolArgs, _round:readRound}];
      const turn = {
        turnId:'synthetic-turn', conversationId:'synthetic-program-chronology',
        laneId:'main', ordinal:1, actor:'assistant', kind:'reply', status:'running',
        currentAttemptId:scope.attemptId, projectionRevision:1, createdAt:1, updatedAt:1,
        projection:{segments:[thinking(11, 'Round eleven'), gateway], toolRounds:[program]},
      };
      let state = {...createTurnState(turn.conversationId),
        turnsById:{[turn.turnId]:turn}, laneOrder:{main:[turn.turnId]}};
      const surface = createConversationSurface(document.getElementById('surface'),
        createClassicConversationRenderers({
          renderSafeMarkdownHtml(value) {
            const paragraph = document.createElement('p');
            paragraph.textContent = value;
            return paragraph.outerHTML;
          },
        }));
      const render = () => surface.render(selectConversationViewModel(state));
      const update = change => {
        state = JSON.parse(JSON.stringify(state));
        const next = state.turnsById[turn.turnId];
        next.projectionRevision += 1;
        next.updatedAt += 1;
        state.conversationRevision += 1;
        change(next);
        render();
      };
      const readers = () => [
        document.querySelector('[data-block-id="thinking-11"] .thinking-block'),
        document.querySelector('[data-block-id="program:gateway-11"] .ptc-card'),
        document.querySelector('[data-block-id="program:gateway-11"] .ptc-code'),
      ];
      let retainedReaders;
      // Only test-owned controls are exposed. All rendering uses public exports
      // from one native owner graph, never private application runtime globals.
      window.programChronology = {
        rememberReaders() { retainedReaders = readers(); },
        readersRetained() {
          return readers().every((node, index) => node === retainedReaders[index] && node.open)
            && document.activeElement === retainedReaders[0].querySelector('summary');
        },
        advance() {
          update(next => {
            next.projection.segments.push(...laterSegments);
            next.projection.segments[1]._round.status = 'done';
            next.projection.toolRounds = [
              {...program, programStatus:'completed', status:'done',
                programResult:{round:11, synthetic:true}}, readRound];
          });
        },
        complete() {
          // Do not invent a final answer: completed/replay must preserve this
          // exact authored process projection, including round-12 narration.
          update(next => {
            next.status = 'completed';
            next.currentAttemptId = null;
            next.settlement = {outcome:'completed'};
          });
        },
        replay() { state = JSON.parse(JSON.stringify(state)); render(); },
        dispose() { surface.dispose(); },
      };
      render();
    }""")

    expected = ['thinking-11', 'program:gateway-11', 'thinking-12',
                'narration-12', 'tool:read-12']

    def assert_timeline(phase, block_ids):
        snapshot = page.evaluate("""() => ({
          ids:[...document.querySelectorAll('#surface .conversation-block:not([data-block-kind="live-status"])')]
            .map(node => node.dataset.blockId),
          thinking:[...document.querySelectorAll('.thinking-block .thinking-text')]
            .map(node => node.textContent.trim()),
          programCards:document.querySelectorAll('.ptc-card').length,
          gatewayDuplicates:document.querySelectorAll('[data-block-id="tool:gateway-11"]').length,
          viewport:document.documentElement.clientWidth,
          scrollWidth:document.documentElement.scrollWidth,
          boxes:[...document.querySelectorAll('#surface .conversation-block:not([data-block-kind="live-status"])')].map(node => {
            const box = node.getBoundingClientRect();
            return {top:box.top, bottom:box.bottom, left:box.left, right:box.right, height:box.height};
          }),
        })""")
        evidence = tmp_path / f'program-chronology-{width}-{phase}'
        evidence.with_suffix('.json').write_text(json.dumps(snapshot, indent=2) + '\n')
        page.screenshot(path=str(evidence.with_suffix('.png')), full_page=True)
        print(f'Program chronology browser evidence: {evidence}')
        assert snapshot['ids'] == block_ids
        assert snapshot['programCards'] == 1
        assert snapshot['gatewayDuplicates'] == 0
        assert snapshot['thinking'] == (['Round eleven'] if len(block_ids) == 2
                                        else ['Round eleven', 'Round twelve'])
        assert snapshot['scrollWidth'] <= snapshot['viewport'] + 1
        assert all(box['height'] > 0 and 0 <= box['left'] < box['right'] <= width + 1
                   for box in snapshot['boxes'])
        assert all(before['bottom'] <= after['top'] + 1
                   for before, after in zip(snapshot['boxes'], snapshot['boxes'][1:]))

    first_thinking = page.locator('[data-block-id="thinking-11"] .thinking-block')
    first_header = first_thinking.locator(':scope > summary')
    program_card = page.locator('.ptc-card')
    program_code = program_card.locator('.ptc-code')
    try:
        assert_timeline('running-initial', expected[:2])
        assert program_card.get_attribute('data-program-attention') == 'running'
        assert not first_thinking.evaluate('(node) => node.open')
        first_header.focus()
        first_header.press('Enter')
        # Running cards start open. Close/reopen through real clicks to make
        # the reader's explicit choice independent of attention-state defaults.
        if program_card.evaluate('(node) => node.open'):
            program_card.locator(':scope > summary').click()
        program_card.locator(':scope > summary').click()
        if not program_code.evaluate('(node) => node.open'):
            program_code.locator(':scope > summary').click()
        first_header.focus()
        page.evaluate('programChronology.rememberReaders()')

        page.evaluate('programChronology.advance()')
        assert_timeline('running-update', expected)
        assert program_card.get_attribute('data-program-attention') == 'completed'
        assert page.evaluate('programChronology.readersRetained()')
        assert page.locator('.conversation-block--tool').count() == 1
        assert 'read_files' in page.locator(
            '[data-block-id="tool:read-12"] summary').first.inner_text()
        assert page.locator('[data-block-id="narration-12"]').inner_text() == (
            'Reading the synthetic round twelve evidence.')
        second_thinking = page.locator('[data-block-id="thinking-12"] .thinking-block')
        assert page.locator('.thinking-block').count() == 2
        assert not second_thinking.evaluate('(node) => node.open')
        second_thinking.locator(':scope > summary').click()
        assert second_thinking.locator('.thinking-text').inner_text() == 'Round twelve'
        assert first_thinking.locator('.thinking-text').inner_text() == 'Round eleven'
        # Independent reader choices must not be coupled by thinking coalescing.
        second_thinking.locator(':scope > summary').click()
        assert first_thinking.evaluate('(node) => node.open')
        first_header.focus()

        page.evaluate('programChronology.complete()')
        assert_timeline('completed', expected)
        assert page.evaluate('programChronology.readersRetained()')
        assert not second_thinking.evaluate('(node) => node.open')
        page.evaluate('() => { for (let i = 0; i < 3; i++) programChronology.replay(); }')
        assert_timeline('replay', expected)
        assert page.evaluate('programChronology.readersRetained()')
        assert not second_thinking.evaluate('(node) => node.open')
    finally:
        page.evaluate('programChronology.dispose()')
    # frontend_visual_page also fails teardown on every uncaught page error.
