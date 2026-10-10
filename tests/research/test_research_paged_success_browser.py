"""Real browser shows complete receipt count through paging and successful settlement."""
import pytest

from tests.research.test_research_workspace import storage as storage
from tests.research.test_research_paged_success import initial_science, complete, commit
from tests.research.test_research_pending_paging import save, apply, receipt, DIRECTION
from tests.e2e.test_e2e_smoke import _wait_app_ready, _SENTINEL
from tests.e2e.test_e2e_smoke import _install_llm_stubs, _disable_open_mode_rate_limit  # noqa: F401
from lib.research.records import persist_record

pytestmark = pytest.mark.visual


def test_paged_receipt_count_remains_visible_when_inline_tail_is_empty(page, storage,
        monkeypatch, assert_no_js_errors, tmp_path):
    import lib.research.capabilities as capabilities
    page.add_init_script("if (location.protocol.startsWith('http')) localStorage.setItem('tofu_ui_lang', 'en');")
    page.reload(); _wait_app_ready(page)
    monkeypatch.setattr(capabilities, 'build_capability_catalog', lambda **kwargs: {
        'contract_version': 'tofu.research-capabilities/v1', 'capabilities': [], 'tools': [], 'fingerprint': ''})
    saved = save(initial_science())
    saved = save(apply(saved, 'many-polls', [receipt('job-B', 'completed', 'poll', f'poll-{n}')
                                          for n in range(63)], storage.client))
    persist_record({'task_id': 'paged-success-browser', 'user_id': 1, 'direction': DIRECTION,
        'lang': 'en', 'status': 'done', 'result': {'survey_md': 'Paged experiment receipts.', 'accepted': [], 'rejected': []}})
    page.evaluate("window.location.hash = '/evolution/research'")
    page.locator('.research-record-item').filter(has_text=DIRECTION).click()
    page.locator('[data-rs-tab="workspace"]').click()
    proof = page.locator('.rsw-run-proof')
    page.wait_for_function("document.querySelector('.rsw-run-proof')?.textContent.includes('65 receipts')")
    editor = page.locator('#researchWorkspace [data-rsw-field="hypothesis"]')
    editor.fill('Unsaved scientific hypothesis')
    assert '65 receipts' in proof.inner_text()
    calls = _SENTINEL['stream_calls']
    saved = commit(storage.client, complete(storage.client, saved), saved)
    page.reload(); _wait_app_ready(page)
    page.locator('.research-record-item').filter(has_text=DIRECTION).click()
    page.locator('[data-rs-tab="workspace"]').click()
    proof = page.locator('.rsw-run-proof')
    page.wait_for_function("document.querySelector('.rsw-run-proof')?.textContent.includes('66 receipts')")
    assert _SENTINEL['stream_calls'] == calls
    proof.scroll_into_view_if_needed()
    page.screenshot(path=str(tmp_path / 'research-paged-receipts-desktop.png'))
