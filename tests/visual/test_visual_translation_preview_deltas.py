"""Render delta previews in Chromium against the local application origin."""
from pathlib import Path
import subprocess

import pytest
from tests._jsdom import require_node_modules

pytestmark = pytest.mark.visual
ROOT = Path(__file__).resolve().parents[2]


def test_translation_deltas_merge_and_coalesce_real_browser_paints(live_server, browser, tmp_path):
    require_node_modules('vite')
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


def test_translation_previews_survive_a_conversation_switch_in_a_real_browser(
        live_server, browser, tmp_path):
    """A preview frame for an unmounted conversation is kept, not painted,
    and renders when the reader returns to that conversation."""
    require_node_modules('vite')
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
            container.id = 'translation-switch-fixture';
            document.body.prepend(container);
            const stateFor = (conversationId, text) => {
              const turnId = conversationId + '-turn';
              return {conversationId, conversationRevision:1, transport:'live',
                turnsById:{[turnId]:{turnId, conversationId, laneId:'main', ordinal:1,
                  actor:'assistant', kind:'reply', runId:'', status:'running',
                  currentAttemptId:conversationId + '-attempt',
                  projection:{segments:[{type:'text', blockId:'text:llm-1', text, llmRound:1}]},
                  projectionRevision:1, settlement:{}, createdAt:1, updatedAt:1}},
                laneOrder:{main:[turnId]}, attemptsById:{}, queueItems:[],
                pendingEventsByTurn:{}, commandPending:{}, liveRoundUsageByTurn:{}};
            };
            const fixture = {paints:[], active:'conversation-a', states:{
              'conversation-a':stateFor('conversation-a', 'Checking the logs'),
              'conversation-b':stateFor('conversation-b', 'Another conversation')}};
            fixture.controller = TranslationPreviewHarness.createConversationSurfaceController({
              isActive:id => id === fixture.active, getContainer:() => container,
              schedule:callback => {const id = requestAnimationFrame(callback);return () => cancelAnimationFrame(id);},
              afterConversationCommit:conversation => {fixture.paints.push(conversation.id);},
            });
            fixture.open = id => {fixture.active = id; fixture.controller.render({id}, fixture.states[id]);};
            fixture.open('conversation-a');
            window.translationSwitchFixture = fixture;
        }''')
        page.wait_for_function('translationSwitchFixture.paints.length === 1')
        page.evaluate("translationSwitchFixture.open('conversation-b')")
        page.wait_for_function('translationSwitchFixture.paints.length === 2')
        page.evaluate('''() => translationSwitchFixture.controller.setTranslationActivity(
            'conversation-a', 'conversation-a-turn',
            {status:'pending', partialByRound:{'text:llm-1':'正在检查日志'}})''')
        page.wait_for_timeout(200)
        assert page.evaluate('translationSwitchFixture.paints') == [
            'conversation-a', 'conversation-b']

        page.evaluate("translationSwitchFixture.open('conversation-a')")
        page.wait_for_function('translationSwitchFixture.paints.length === 3')
        text = page.evaluate(
            "document.getElementById('translation-switch-fixture').textContent")
        assert '正在检查日志' in text, text
        page.screenshot(path=str(tmp_path / 'translation-switch.png'), full_page=True)
        page.evaluate('translationSwitchFixture.controller.dispose()')
    finally:
        page.close()
