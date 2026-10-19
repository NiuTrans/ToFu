"""Real local Chromium: authored messages replace bookkeeping, not proof states."""
import json

import pytest

from tests._runtime_sections import ROOT, native_module_path
from tests.support.frontend_visual import frontend_visual_origin, frontend_visual_page as frontend_visual_page  # noqa: F401

pytestmark = pytest.mark.visual


def _fixture(page, *, legacy=False, failed=False, stable=False, submission_state=None, saved=False):
    for name, source in [
        ('send-message-presenter', 'frontend/src/conversation/presentation/tool-result-presentation.ts'),
        ('send-message-renderer', 'frontend/src/conversation/ui/classic-conversation-renderers.ts'),
    ]:
        page.add_script_tag(path=native_module_path(f'.native/{name}.js', ROOT / source))
    locale = json.loads((ROOT / 'frontend/src/i18n/locales/en.json').read_text())
    contract = json.loads((ROOT / 'contracts/tool_result_surfaces_v1.json').read_text())
    content = 'MESSAGE HEAD\nCheck <script>window.messageInjected=true</script> & preserve every line.\n' + 'Evidence 世界. ' * 70 + '\nMESSAGE TAIL'
    page.evaluate(r'''({locale,outcome,content,legacy,failed,stable,submission_state,saved}) => {
      document.body.innerHTML='<main id="fixture" class="conversation-surface"><div id="tool"></div></main>';
      const translate=(key,args)=>(locale[key]||key).replace(/\{(\w+)\}/g,(_,name)=>args?.[name]??'');
      const presenter=createToolResultPresentation({translate,
        writeGateRefusal:{resolveRefusal:()=>null,renderNoticeHtml:()=>''}});
      const header={iconHtml:'',queryHtml:'Send Agent Message',rootPillHtml:'',badgeHtml:'OLD RECEIPT'};
      const args=stable
        ? {retained_agent_id:'persistent-colleague',scope:'project',intent:'message',content,kind:'reference',idempotency_key:'stable-key'}
        : {agent_id:'dispatch-frontend',swarm_run_id:'owned-run',content,kind:'steer',idempotency_key:'stable-key'};
      const assignment={agent_id:'ordinary-child',swarm_run_id:'new-owned-run',conversation_id:'child-dialogue'};
      const launch={status:'async_launched',swarm_key:'ordinary-swarm',agents:[{
        id:assignment.agent_id,swarm_run_id:assignment.swarm_run_id,conversation_id:assignment.conversation_id,task_id:'execution-task'}]};
      const receipt=stable
        ? {retained_agent_id:args.retained_agent_id,scope:args.scope,message_id:'stable-message',
          state:saved?'unconfirmed':'turn_started',kind:args.kind,contentChars:Array.from(content).length,
          execution_started:!saved,assignment:saved?assignment:{...assignment,launch}}
        : {agent_id:args.agent_id,swarm_run_id:args.swarm_run_id,message_id:'receipt-id',
          state:'submitted',kind:args.kind,contentChars:Array.from(content).length,execution_started:false};
      const error={status:'error',code:'message_storage_failed',message:'Acknowledgement uncertain <script>bad()</script>',
        error:'Acknowledgement uncertain <script>bad()</script>',
        next_action:'Query the exact run receipt; reuse the SAME idempotency_key and unchanged content.'};
      if (submission_state) error.submission_state=submission_state;
      if (saved) error.receipt=receipt;
      if (stable) error.next_action='Inspect the SAME stable key and pinned assignment; do not transfer to replacement work.';
      window.round={roundNum:1,toolName:'send_agent_message',toolCallId:'send-message',status:failed?'error':'done',
        toolArgs:args,toolContent:failed?JSON.stringify(error):(legacy||stable)?JSON.stringify(receipt):outcome};
      const renderer=createClassicConversationRenderers({
        localizedText:(key,fallback)=>locale[key]||fallback,
        renderToolBlockHtml:block=>presenter.renderGenericResultHtml(block.round,{},header),
      });
      const node=document.querySelector('#tool');
      window.repaint=()=>renderer.renderBlock(node,{kind:'tool',name:'send_agent_message',round},{turn:{turnId:'fixture-turn'}});
      repaint();
    }''', {'locale': locale, 'outcome': contract['sendMessageReceipt']['outcomes']['submitted'],
           'content': content, 'legacy': legacy, 'failed': failed, 'stable': stable,
           'submission_state': submission_state, 'saved': saved})
    page.add_style_tag(content='body{display:block;overflow:auto}#fixture{padding:16px;max-width:100%}')
    return content


@pytest.mark.parametrize('width', [1100, 390, 320])
@pytest.mark.parametrize('legacy', [False, True])
def test_full_authored_message_without_receipt_json_and_reader_owned_fold(frontend_visual_page, width, legacy, tmp_path):
    page = frontend_visual_page
    page.set_viewport_size({'width': width, 'height': 900})
    content = _fixture(page, legacy=legacy)
    card = page.locator('.ptool-agent-message')
    assert card.locator('code').inner_text() == content
    assert 'Message to dispatch-frontend' in card.locator('summary').inner_text()
    assert 'Submitted' in card.locator('.ptool-line-status').inner_text()
    for junk in ('contentChars', 'execution_started', 'receipt-id', 'owned-run', 'OLD RECEIPT'):
        assert junk not in card.inner_text()
    assert page.locator('#tool script').count() == 0
    assert not page.evaluate('Boolean(window.messageInjected)')
    summary = card.locator('summary')
    summary.focus()
    page.keyboard.press('Enter')
    assert not card.locator('.ptool-result-body').is_visible()
    page.evaluate('repaint()')
    assert not card.locator('.ptool-result-body').is_visible()
    assert summary.evaluate('node=>node===document.activeElement')
    page.keyboard.press('Space')
    assert card.locator('code').inner_text() == content
    for theme in ('dark', 'light', 'tofu'):
        page.evaluate('theme=>document.documentElement.dataset.theme=theme', theme)
        assert not page.evaluate('document.documentElement.scrollWidth > innerWidth')
        assert card.locator('.ptool-text').evaluate('node=>node.getBoundingClientRect().width') >= 190
    page.screenshot(path=str(tmp_path / f'send-message-{width}-{legacy}.png'))


def test_failure_keeps_authored_body_and_exact_reconciliation_without_claiming_sent(frontend_visual_page):
    page = frontend_visual_page
    page.set_viewport_size({'width': 390, 'height': 900})
    content = _fixture(page, failed=True)
    card = page.locator('.ptool-agent-message.ptool-error')
    assert card.locator('code').first.inner_text() == content
    assert 'Send failed' in card.locator('summary').inner_text()
    error = card.locator('.ptool-agent-message-error').inner_text()
    assert 'Acknowledgement uncertain <script>bad()</script>' in error
    assert 'SAME idempotency_key and unchanged content' in error
    assert 'Submitted' not in card.inner_text()
    assert page.locator('#tool script').count() == 0
    assert not page.evaluate('document.documentElement.scrollWidth > innerWidth')


@pytest.mark.parametrize('width', [1100, 390, 320])
@pytest.mark.parametrize(('submission_state', 'saved', 'label'), [
    (None, False, 'New turn accepted · input not confirmed'),
    ('rejected', False, 'Send rejected · not submitted'),
    ('unknown', False, 'Send failed · submission may be uncertain'),
    ('unknown', True, 'Message saved · input not confirmed'),
])
def test_stable_target_turn_and_submission_states(frontend_visual_page, width, submission_state, saved, label, tmp_path):
    page = frontend_visual_page
    page.set_viewport_size({'width': width, 'height': 900})
    content = _fixture(page, stable=True, failed=submission_state is not None,
                       submission_state=submission_state, saved=saved)
    card = page.locator('.ptool-agent-message')
    assert card.locator('code').first.inner_text() == content
    assert 'Message to persistent-colleague' in card.locator('summary').inner_text()
    assert card.locator('.ptool-line-status').inner_text() == label
    assert card.locator('.ptool-line-status--ok').count() == 0
    assert 'Input consumed' not in card.locator('summary').inner_text()
    for junk in ('contentChars', 'execution_started', 'async_launched', 'ordinary-child', 'new-owned-run', 'execution-task'):
        assert junk not in card.inner_text()
    if submission_state is not None:
        diagnostic = card.locator('.ptool-agent-message-error').inner_text()
        assert 'message_storage_failed' in diagnostic
        assert 'Acknowledgement uncertain <script>bad()</script>' in diagnostic
        assert 'SAME stable key and pinned assignment' in diagnostic
    if saved:
        assert 'Do not transfer this message to a replacement run.' in card.inner_text()
    assert page.locator('#tool script').count() == 0
    assert not page.evaluate('Boolean(window.messageInjected)')
    assert not page.evaluate('document.documentElement.scrollWidth > innerWidth')
    page.screenshot(path=str(tmp_path / f'stable-message-{width}-{submission_state}-{saved}.png'))


@pytest.mark.parametrize('state', ['input_confirmed', 'unconfirmed'])
def test_receipt_update_preserves_authored_body_and_reader_focus(frontend_visual_page, state):
    page = frontend_visual_page
    content = _fixture(page, legacy=True)
    card = page.locator('.ptool-agent-message')
    summary = card.locator('summary')
    summary.focus()
    page.keyboard.press('Enter')
    assert not card.locator('.ptool-result-body').is_visible()
    page.evaluate('''state => {
      const receipt=JSON.parse(round.toolContent);
      receipt.state=state;
      round.toolContent=JSON.stringify(receipt);
      repaint();
    }''', state)
    label = card.locator('.ptool-line-status').inner_text()
    assert ('Input consumed' if state == 'input_confirmed' else 'Run ended') in label
    assert 'Submitted' not in label
    assert not card.locator('.ptool-result-body').is_visible()
    assert summary.evaluate('node=>node===document.activeElement')
    page.keyboard.press('Enter')
    assert card.locator('code').inner_text() == content
    assert page.locator('#tool script').count() == 0
