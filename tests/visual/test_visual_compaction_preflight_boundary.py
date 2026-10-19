"""A refused mid-turn preflight stays after its witnessed output in Chromium.

Uses the real backend event fold and frontend surface graph on an isolated
local HTTP server. Synthetic evidence never writes a user conversation.
"""
import json
import subprocess

import pytest

from lib.agent_core.events import EventType, build_event
from lib.turn_activity_timeline import capture_compaction_boundary, fold_activity_timeline
from tests._runtime_sections import ROOT
from tests.support.frontend_visual import frontend_visual_origin, frontend_visual_page as frontend_visual_page  # noqa: F401

pytestmark = pytest.mark.visual


@pytest.mark.parametrize('width', [1100, 390])
def test_refused_preflight_follows_its_tool_in_live_and_reload(frontend_visual_page, tmp_path, width):
    page = frontend_visual_page
    page.set_viewport_size({'width': width, 'height': 900})
    bundle = tmp_path / 'conversation-owner.js'
    result = subprocess.run([
        str(ROOT / 'scripts/vite_test_bundle.mjs'),
        str(ROOT / 'frontend/src/conversation/index.ts'), '--bundle', '--format=iife',
        '--platform=browser', '--global-name=ConversationOwner', f'--outfile={bundle}',
    ], cwd=ROOT, capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    page.add_script_tag(content=bundle.read_text())
    identity = {'taskId': 'preflight-task', 'attemptId': 'preflight-attempt'}
    segments = [
        {**identity, 'type': 'text', 'blockId': 'earlier', 'llmRound': 0,
         'text': 'Earlier work is complete.'},
        {**identity, 'type': 'tool_use', 'blockId': 'last-edit', 'id': 'edit-call',
         'name': 'edit_file', 'roundNum': 48, 'llmRound': 22},
    ]
    owner = {'id': identity['taskId'], '_attemptId': identity['attemptId'], 'segments': segments}
    event = build_event(EventType.COMPACTION_DONE,
        archiveId='summary-failed:preflight:23', convId='fixture', trigger='dispatch_guard',
        roundNum=23, tokensBefore=89020, tokensAfter=89020, tokenCountKind='estimated',
        msgsBefore=80, msgsAfter=80, reductionPct=0,
        receipt={'status': 'failed', 'outcomeReason': 'irreducible_prompt_floor'},
        **capture_compaction_boundary({}, owner))
    timeline = fold_activity_timeline(None, {**event, 'seq': 100, 'emittedAt': 1000}, owner)
    segments.append({**identity, 'type': 'text', 'blockId': 'later', 'llmRound': 23,
                     'text': 'The request could not continue.', 'terminal': True, 'deliverable': True})
    projection = {'segments': segments, 'activityTimeline': timeline,
                  'content': 'The request could not continue.', 'toolRounds': [{
                      **identity, 'toolCallId': 'edit-call', 'toolName': 'edit_file',
                      'roundNum': 48, 'llmRound': 22, 'status': 'done',
                      'toolContent': 'Last edit completed.'}]}
    labels = json.loads((ROOT / 'frontend/src/i18n/locales/en.json').read_text())
    for mode in ('live', 'reload'):
        page.evaluate(r'''({projection,labels,mode}) => {
          window.boundaryController?.dispose();
          document.body.innerHTML='<main id="fixture" style="max-width:800px;padding:16px;margin:auto"></main>';
          const turn={turnId:'fixture-turn',presentationId:'fixture-turn',conversationId:'fixture',
            laneId:'main',ordinal:1,actor:'assistant',kind:'message',status:'failed',
            currentAttemptId:'preflight-attempt',projectionRevision:1,projection,
            settlement:{outcome:'failed',cause:'generation_error',evidence:'generation_error'},
            createdAt:100,updatedAt:1000};
          const state={conversationId:'fixture',conversationRevision:1,transport:'live',
            turnsById:{'fixture-turn':turn},laneOrder:{main:['fixture-turn']},attemptsById:{},
            queueItems:[],pendingEventsByTurn:{},commandPending:{},liveRoundUsageByTurn:{}};
          const renderers=ConversationOwner.createClassicConversationRenderers({
            localizedText:(key,fallback,values={})=>(labels[key]||fallback)
              .replace(/\{(\w+)\}/g,(_,name)=>values[name]??''),
            renderToolBlockHtml:()=>'<div id="last-tool-output">Last edit completed.</div>',
            renderSafeMarkdownHtml:text=>{const p=document.createElement('p');p.textContent=text;return p.outerHTML;},
          });
          window.boundaryController=ConversationOwner.createConversationSurfaceController({
            isActive:id=>id==='fixture',getContainer:()=>document.getElementById('fixture'),
            nativeRenderers:renderers,
            schedule:cb=>{const id=requestAnimationFrame(cb);return()=>cancelAnimationFrame(id);},
          });
          window.boundaryController.render({id:'fixture'},mode==='reload'?JSON.parse(JSON.stringify(state)):state);
        }''', {'projection': projection, 'labels': labels, 'mode': mode})
        card = page.locator('.activity-event--compaction')
        assert card.count() == 1
        assert card.bounding_box()['y'] > page.locator('#last-tool-output').bounding_box()['y']
        assert card.bounding_box()['y'] < page.get_by_text('The request could not continue.', exact=True).bounding_box()['y']
        assert not page.evaluate('document.documentElement.scrollWidth > innerWidth')
        page.screenshot(path=str(tmp_path / f'preflight-{width}-{mode}.png'))
