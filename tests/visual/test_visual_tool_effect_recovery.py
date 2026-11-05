"""Real tool/program renderers preserve unknown-effect warnings on restore and repaint."""
import json

import pytest

from tests._runtime_sections import native_module_path
from tests.support.frontend_visual import ROOT, frontend_visual_origin, frontend_visual_page as frontend_visual_page  # noqa: F401

pytestmark = pytest.mark.visual


@pytest.mark.parametrize('theme,width,locale', [('light', 390, 'en'), ('dark', 1180, 'zh'), ('tofu', 390, 'zh')])
def test_unknown_effect_is_visible_without_opening_results_and_survives_repaint(
        frontend_visual_page, theme, width, locale, tmp_path):
    from playwright.sync_api import expect
    page = frontend_visual_page
    page.set_viewport_size({'width': width, 'height': 950})
    page.add_script_tag(path=native_module_path('.native/tool-effect-recovery.js',
        ROOT / 'frontend/src/conversation/ui/renderers/tool-blocks.ts'))
    messages = json.loads((ROOT / f'frontend/src/i18n/locales/{locale}.json').read_text())
    page.evaluate('''({theme,messages})=>{
      document.documentElement.dataset.theme=theme;
      document.body.innerHTML='<main class="conversation-surface" id="fixture"><div id="tool"></div><div id="program"></div></main>';
      window.effect={operationId:'a'.repeat(32),state:'entered',recovery:'reconcile',scope:'attempt'};
      window.block={id:'tool-1',kind:'tool',name:'perform',toolCallId:'call',input:{},
        round:{roundNum:1,toolName:'perform',status:'done',executionEffect:effect},result:{status:'done',content:'response unavailable'}};
      window.turn={turnId:'turn',status:'interrupted',source:{conversationId:'conversation'}};
      const ports={localizedText:(key,fallback)=>messages[key]??fallback,
        renderToolBlockHtml:()=>'<details class="ptool-result-block"><summary class="ptool-line">Tool evidence</summary><pre>Previous response</pre></details>'};
      window.paint=()=>{
        renderToolBlock(document.querySelector('#tool'),block,turn,ports);
        renderProgramBlock(document.querySelector('#program'),{...block,kind:'program'},turn,ports);
      };
      paint();
    }''', {'theme': theme, 'messages': messages})
    page.add_style_tag(content='body{display:block;height:auto;overflow:auto;margin:0;padding:var(--space-4);background:var(--bg-primary)}#fixture{max-width:var(--conversation-reading-measure);margin-inline:auto;}')
    notice = page.locator('#tool > [data-tool-effect-notice]')
    expect(notice).to_contain_text(messages['tool.effectUnknown'])
    expect(notice).to_contain_text(messages['tool.effectReconcile'])
    expect(page.locator('#program > [data-tool-effect-notice]')).to_be_visible()
    expect(page.locator('#tool details')).not_to_have_attribute('open', '')
    page.locator('#tool summary').press('Enter')
    page.locator('#tool summary').focus()
    page.evaluate('''()=>{window.oldNotice=document.querySelector('#tool > [data-tool-effect-notice]');window.oldSummary=document.activeElement;paint();}''')
    assert page.evaluate('document.activeElement===oldSummary')
    assert page.evaluate('document.querySelector("#tool > [data-tool-effect-notice]")===oldNotice')
    expect(page.locator('#tool details')).to_have_attribute('open', '')
    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1')
    page.screenshot(path=str(tmp_path / f'tool-effect-{theme}-{width}.png'), full_page=True)
    page.evaluate("effect.state='returned';paint()")
    expect(page.locator('[data-tool-effect-notice]')).to_have_count(0)
    page.evaluate("effect.state='unknown';effect.recovery='idempotent';turn.status='running';paint()")
    expect(notice).to_contain_text(messages['tool.effectIdempotent'])
    page.evaluate("effect.recovery='read_only';paint()")
    expect(page.locator('[data-tool-effect-notice]')).to_have_count(0)


def test_live_surface_repaints_on_status_only_transition(frontend_visual_page, tmp_path):
    from playwright.sync_api import expect
    page = frontend_visual_page
    entry = tmp_path / 'effect-live.ts'
    entry.write_text('\n'.join('export * from '+json.dumps(str(ROOT / path))+';' for path in [
        'frontend/src/conversation/index.ts', 'frontend/src/core/turn-state.ts']))
    page.add_script_tag(path=native_module_path('.native/tool-effect-live.js', entry))
    page.evaluate('''()=>{
      document.body.innerHTML='<main id="surface" class="conversation-surface"></main>';
      const effect={operationId:'a'.repeat(32),state:'entered',recovery:'reconcile',scope:'attempt'};
      const round={toolName:'perform',toolCallId:'call',roundNum:1,status:'running',executionEffect:effect};
      const turn={turnId:'answer',conversationId:'effects',actor:'assistant',laneId:'main',ordinal:1,
        status:'running',projectionRevision:1,projection:{segments:[
          {type:'tool_use',blockId:'call',id:'call',name:'perform',input:{},_round:round}]}};
      window.state={...createTurnState('effects'),turnsById:{answer:turn},laneOrder:{main:['answer']}};
      const ports={renderSafeMarkdownHtml:()=>'',renderToolBlockHtml:()=>
        '<details class="ptool-result-block"><summary class="ptool-line">Tool evidence</summary><pre>Evidence</pre></details>'};
      window.surface=createConversationSurface(document.querySelector('#surface'),createClassicConversationRenderers(ports));
      window.paint=()=>surface.render(selectConversationViewModel(state));paint();
    }''')
    expect(page.locator('[data-tool-effect-notice]')).to_have_count(0)
    page.locator('.ptool-result-block summary').focus()
    page.evaluate("window.previousSummary=document.activeElement;state.turnsById.answer={...state.turnsById.answer,status:'interrupted',projectionRevision:2};paint()")
    expect(page.locator('[data-tool-effect-notice]')).to_be_visible()
    expect(page.locator('[data-tool-effect-notice]')).to_contain_text('Operation outcome unknown')
    assert page.evaluate('document.activeElement===previousSummary')
    page.evaluate('surface.dispose()')


@pytest.mark.parametrize('locale,width', [('zh', 390), ('en', 1180)])
def test_command_outcome_chrome_matches_notice_and_retains_diagnostics(
        frontend_visual_page, locale, width, tmp_path):
    from playwright.sync_api import expect
    page = frontend_visual_page
    page.set_viewport_size({'width': width, 'height': 950})
    entry = tmp_path / 'command-outcome.ts'
    entry.write_text('\n'.join('export * from ' + json.dumps(str(ROOT / path)) + ';' for path in [
        'frontend/src/conversation/ui/renderers/tool-blocks.ts',
        'frontend/src/conversation/presentation/tool-header-presentation.ts',
        'frontend/src/conversation/presentation/tool-result-presentation.ts',
        'frontend/src/conversation/presentation/view-model/activity-blocks.ts',
    ]))
    page.add_script_tag(path=native_module_path('.native/command-outcome.js', entry))
    messages = json.loads((ROOT / f'frontend/src/i18n/locales/{locale}.json').read_text())
    page.evaluate('''messages => {
      document.body.innerHTML='<main class="conversation-surface"><div id="not_started"></div><div id="unknown"></div></main>';
      const translate=key=>messages[key]??key;
      const writeGateRefusal={resolveRefusal:()=>null,renderBadgeHtml:()=>'',renderNoticeHtml:()=>''};
      const header=createToolHeaderPresentation({translate,writeGateRefusal});
      const results=createToolResultPresentation({translate,writeGateRefusal});
      const ports={localizedText:(key,fallback)=>messages[key]??fallback,
        renderToolBlockHtml:block=>results.renderGenericResultHtml(block.round,{}, {
          iconHtml:'',queryHtml:'git log -1; git branch --show-current; git status --short | head',
          rootPillHtml:'',badgeHtml:header.renderBadgeHtml(block.round,{})})};
      for(const state of ['not_started','unknown']) {
        const round={toolName:'run_command',roundNum:28,status:'error',
          executionEffect:{operationId:'a'.repeat(32),state,recovery:'reconcile',scope:'attempt'},
          toolContent:JSON.stringify({code:state==='unknown'?'tool_effect_unknown':'tool_execution_failed',
            status:'error',retryable:false,message:state==='unknown'
              ?'Workspace tree lifecycle write is active [holder: pid=123]'
              :'Error: tool "run_command" execution failed with CommandAdmissionRefusal: Error [git-inspection-preparation]: Git worktree lifecycle requires literal operands. Command was not started.. Arguments received: {"command":"git worktree remove \\"$WT\\""}'})};
        const block={id:state,kind:'tool',name:'run_command',toolCallId:state,input:{},round};
        renderToolBlock(document.getElementById(state),block,{turnId:'turn',status:'running',source:{}},ports);
      }
    }''', messages)
    page.add_style_tag(content='body{display:block;height:auto;overflow:auto;margin:0;padding:var(--space-4);background:var(--bg-primary)}main{max-width:var(--conversation-reading-measure);margin-inline:auto;}')
    known = page.locator('#not_started')
    unknown = page.locator('#unknown')
    expect(known.locator('summary')).to_contain_text(messages['tool.notStarted'])
    expect(known.locator('summary')).to_contain_text('Git worktree lifecycle requires literal operands')
    expect(known.locator('summary')).not_to_contain_text('Arguments received')
    expect(known.locator('[data-tool-effect-notice]')).to_have_count(0)
    expect(unknown.locator('summary')).to_contain_text(messages['tool.effectUnknown'])
    expect(unknown.locator('[data-tool-effect-notice]')).to_contain_text(messages['tool.effectReconcile'])
    # Named once by the header chip; neither a reason line nor the notice repeats it.
    expect(unknown.locator('.ptool-artifact-failure')).to_have_count(0)
    expect(unknown.locator('[data-tool-effect-notice]')).not_to_contain_text(messages['tool.effectUnknown'])
    for card, token in ((known, '--text-secondary'),):
        assert card.locator('.ptool-artifact-failure').evaluate('''(node,token)=>{
          const probe=document.createElement('span');probe.style.color=`var(${token})`;node.append(probe);
          const matches=getComputedStyle(node).color===getComputedStyle(probe).color;probe.remove();return matches;
        }''', token)
    for card in (known, unknown):
        expect(card.locator('summary')).not_to_contain_text(messages['tool.failedTip'])
        expect(card.locator('summary .ptool-badge')).not_to_have_text(messages['tool.failed'])
        expect(card.locator('summary')).not_to_contain_text('pid=123')
        expect(card.locator('details')).not_to_have_attribute('open', '')
    page.screenshot(path=str(tmp_path / f'command-outcome-{locale}-{width}.png'), full_page=True)
    # The screenshot incident: an unknown-effect run_command plus the activity
    # row its lost completion frame left behind. The card names the outcome
    # once and the timeline does not trail a stale "run_command started".
    from lib.turn_activity_timeline import fold_activity_timeline
    task = {'_attemptId': 'attempt-a', 'id': 'task-a'}
    timeline = None
    for event in (
            {'type': 'tool_start', 'seq': 1, 'emittedAt': 1_000,
             'toolCallId': 'unknown', 'toolName': 'run_command', 'roundNum': 28},
            {'type': 'done', 'seq': 2, 'emittedAt': 46_000, 'finishReason': 'aborted'}):
        timeline = fold_activity_timeline(timeline, event, task)
    entry = timeline['entries'][0]
    assert entry['summaryKey'] == 'activity.tool.aborted'
    rows = page.evaluate('''entry => {
      const block={id:'unknown',kind:'tool',blockId:'tool-unknown',identitySource:'contract',
        source:{blockId:'tool-unknown'},name:'run_command',toolCallId:'unknown',input:{},result:{},
        round:{toolName:'run_command',roundNum:28,toolCallId:'unknown',status:'error',
          executionEffect:{operationId:'a'.repeat(32),state:'unknown',recovery:'reconcile',scope:'attempt'}}};
      return visibleActivityEntries({blockId:'timeline',version:1,entries:[entry]},[],[block]);
    }''', entry)
    assert rows == [], 'the settled card already owns this outcome'
    started = messages['activity.tool.started'].format(tool='run_command')
    stopped = messages['activity.tool.aborted'].format(tool='run_command')
    assert page.locator('#unknown').inner_text().count(messages['tool.effectUnknown']) == 1
    assert started not in page.locator('#unknown').inner_text()
    assert stopped not in page.locator('#unknown').inner_text()
    known.locator('summary').press('Enter')
    expect(known.locator('.ptool-result-body')).to_contain_text('Arguments received')
    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1')
