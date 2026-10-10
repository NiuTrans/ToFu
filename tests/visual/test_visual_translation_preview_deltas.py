"""Render delta previews in Chromium against the local application origin."""
from pathlib import Path
import subprocess

import pytest

pytestmark = pytest.mark.visual
ROOT = Path(__file__).resolve().parents[2]


def test_translation_deltas_merge_and_coalesce_real_browser_paints(live_server, browser, tmp_path):
    bundle = tmp_path / 'conversation.js'
    result = subprocess.run([str(ROOT / 'scripts/vite_test_bundle.mjs'),
        str(ROOT / 'frontend/src/conversation/index.ts'), '--bundle', '--format=iife',
        '--platform=browser', '--global-name=TranslationPreviewHarness', f'--outfile={bundle}'],
        cwd=ROOT, capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    page = browser.new_page()
    try:
        page.goto(live_server, wait_until='domcontentloaded')
        page.add_script_tag(content=bundle.read_text())
        page.evaluate('''() => {
            const container = document.createElement('main');
            container.id = 'translation-preview-fixture';
            document.body.prepend(container);
            const turn = {turnId:'fixture-turn', conversationId:'fixture-conversation',
              laneId:'main', ordinal:1, actor:'assistant', kind:'reply', runId:'', status:'running',
              currentAttemptId:'fixture-attempt',
              projection:{content:'Source response', segments:[
                {type:'text', blockId:'text:llm-1', text:'First source', llmRound:1},
                {type:'thinking', blockId:'thinking:llm-1', text:'Reason source', llmRound:1},
                {type:'text', blockId:'text:llm-2', text:'Second source', llmRound:2},
              ]}, projectionRevision:1, settlement:{}, createdAt:1, updatedAt:1};
            const state = {conversationId:'fixture-conversation',conversationRevision:1,
              transport:'live',turnsById:{'fixture-turn':turn},laneOrder:{main:['fixture-turn']},
              attemptsById:{},queueItems:[],pendingEventsByTurn:{},commandPending:{},liveRoundUsageByTurn:{}};
            const fixture = {paints:0, before:JSON.stringify(state), state};
            fixture.controller = TranslationPreviewHarness.createConversationSurfaceController({
              isActive:id => id === 'fixture-conversation',getContainer:() => container,
              schedule:callback => {const id = requestAnimationFrame(callback);return () => cancelAnimationFrame(id);},
              afterConversationCommit:(_conversation,_state,_force,model) => {fixture.paints++;fixture.model=model;},
            });
            fixture.controller.render({id:'fixture-conversation'},state);
            window.translationPreviewFixture = fixture;
        }''')
        page.wait_for_function('translationPreviewFixture.paints === 1')
        page.evaluate('''() => {
            const fixture = translationPreviewFixture;
            for (const [key, value] of [['text:llm-1','First translation'],
              ['thinking:llm-1','Reason translation'],['text:llm-2','Second translation'],
              ['text:llm-1','Updated first translation']]) {
              fixture.controller.setTranslationActivity('fixture-conversation','fixture-turn',
                {status:'pending',partialByRound:{[key]:value}});
            }
        }''')
        page.wait_for_function('translationPreviewFixture.paints === 2')
        result = page.evaluate('''() => ({
          text:document.getElementById('translation-preview-fixture').textContent,
          unchanged:translationPreviewFixture.before === JSON.stringify(translationPreviewFixture.state),
          paints:translationPreviewFixture.paints,
          blocks:translationPreviewFixture.model.mainLane.turns[0].blocks.map(block => ({id:block.blockId,kind:block.kind,display:block.displayMarkdown,mode:block.displayMode})),
        })''')
        assert 'Updated first translation' in result['text'], result
        assert 'Second translation' in result['text']
        assert result['unchanged'] and result['paints'] == 2
        page.screenshot(path=str(tmp_path / 'translation-preview-deltas.png'), full_page=True)
        page.evaluate('translationPreviewFixture.controller.dispose()')
        assert page.locator('#translation-preview-fixture [data-conversation-surface]').count() == 0
    finally:
        page.close()
