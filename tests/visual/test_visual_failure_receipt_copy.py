"""Real browser copy fallback preserves document ownership, focus and cleanup."""
import pytest

from tests._runtime_sections import ROOT, native_module_path
from tests.support.frontend_visual import frontend_visual_origin, frontend_visual_page as frontend_visual_page  # noqa: F401

pytestmark = pytest.mark.visual


@pytest.mark.parametrize('verdict', ['throw', 'false', 'true'])
def test_failure_receipt_copy_uses_its_document_and_restores_focus(frontend_visual_page, verdict):
    from playwright.sync_api import expect

    page = frontend_visual_page
    page.add_script_tag(path=native_module_path(
        '.native/failure-receipt-copy.js',
        ROOT / 'frontend/src/conversation/ui/renderers/failure-receipt.ts'))
    page.evaluate("""verdict => {
      window.foreignCopies = 0;
      Object.defineProperty(navigator, 'clipboard', {value: {writeText: () => {
        window.foreignCopies += 1; return Promise.resolve();
      }}});
      const iframe = document.createElement('iframe');
      iframe.name = 'receipt-document'; document.body.append(iframe);
      const owned = iframe.contentDocument;
      owned.body.innerHTML = '<main id="receipt"></main>';
      Object.defineProperty(iframe.contentWindow.navigator, 'clipboard', {value: undefined});
      iframe.contentWindow.copyAttempts = 0;
      Object.defineProperty(owned, 'execCommand', {value: () => {
        iframe.contentWindow.copyAttempts += 1;
        iframe.contentWindow.copiedDiagnostics = JSON.parse(owned.querySelector('textarea').value);
        owned.querySelector('textarea').focus();
        if (verdict === 'throw') throw Error('Synthetic compatibility denial');
        return verdict === 'true';
      }});
      const receipt = buildFailureReceipt(owned,
        {kind:'internal',message:'Synthetic failure',retryable:false, hintKey:'internal.hint'},
        {turnId:'turn',attemptId:'attempt',taskId:'task',status:'failed',
          source:{settlement:{outcome:'failed',cause:'generation_error',
            error:{message:'duplicate'},resumeOptions:[{anchor:{content:'PRIVATE'}}]}}},
        {renderSafeMarkdownHtml: () => {throw Error('Must remain plain text');}});
      owned.querySelector('#receipt').append(receipt);
    }""", verdict)
    frame = page.frame(name='receipt-document')
    button = frame.locator('.activity-event__copy-diagnostics')
    button.focus()
    for attempts in (1, 2):
        button.press('Enter')
        frame.wait_for_function('expected => window.copyAttempts === expected', arg=attempts)
        expect(button).to_have_text('Copied' if verdict == 'true' else 'Copy diagnostics')
        expect(frame.locator('textarea')).to_have_count(0)
        assert frame.evaluate('document.activeElement === document.querySelector("button")')
        assert page.evaluate('window.foreignCopies') == 0

    diagnostics = frame.evaluate('window.copiedDiagnostics')
    assert diagnostics['turnId'] == 'turn'
    assert 'hintKey' not in diagnostics['error']
    assert diagnostics['settlement'] == {'outcome': 'failed', 'cause': 'generation_error'}
