"""Live workbench history paging preserves drafts and reads no model."""
import pytest
from tests.research.test_research_workspace import storage as storage
from tests.research.test_research_history import draft_with_runs, observation, DIRECTION, save
from tests.e2e.test_e2e_smoke import _wait_app_ready, _SENTINEL
from tests.e2e.test_e2e_smoke import _install_llm_stubs, _disable_open_mode_rate_limit  # noqa: F401
from lib.research.records import persist_record

pytestmark = pytest.mark.visual


def test_read_archived_falsifier_without_overwriting_draft(page, storage, monkeypatch, assert_no_js_errors, tmp_path):
    import lib.research.capabilities as capabilities
    _wait_app_ready(page)
    monkeypatch.setattr(capabilities, 'build_capability_catalog', lambda **_: {
        'contract_version': 'tofu.research-capabilities/v1', 'capabilities': [], 'tools': [], 'fingerprint': ''})
    saved = save(draft_with_runs(24, negative=0))
    for index in range(24, 40):
        saved['runs'].append(observation(saved, index))
        saved = save(saved)
    persist_record({'task_id': 'history-browser-fixture', 'user_id': 1,
        'direction': DIRECTION, 'lang': 'en', 'status': 'done',
        'result': {'survey_md': 'Archived research observations.', 'accepted': [], 'rejected': []}})
    page.evaluate("window.location.hash = '/evolution/research'")
    page.locator('.research-record-item').filter(has_text=DIRECTION).click()
    page.locator('[data-rs-tab="workspace"]').click()
    host = page.locator('#researchExperimentHistory')
    host.wait_for()
    calls = _SENTINEL['stream_calls']
    editor = page.locator('#researchWorkspace [data-rsw-field="hypothesis"]')
    editor.fill('Unsaved scientific scope edit')
    host.get_by_role('button').first.click()
    host.locator('article').first.wait_for()
    assert host.locator('article').count() <= 16
    host.get_by_role('button').nth(1).click()
    page.wait_for_function("document.querySelector('#researchExperimentHistory strong')?.textContent.includes('run-8')")
    host.get_by_role('button').nth(1).click()
    page.wait_for_function("document.querySelector('#researchExperimentHistory strong')?.textContent.includes('run-0')")
    assert 'refuted' in host.locator('article').first.inner_text()
    host.locator('summary').first.click()
    assert 'metrics/0.json' in host.locator('pre').first.inner_text()
    assert editor.input_value() == 'Unsaved scientific scope edit'
    assert _SENTINEL['stream_calls'] == calls
    assert not host.locator('input,textarea').count()
    page.set_viewport_size({'width': 390, 'height': 844})
    host.scroll_into_view_if_needed()
    assert host.evaluate('(el) => el.scrollWidth <= el.clientWidth + 1')
    page.screenshot(path=str(tmp_path / 'research-history-mobile.png'))
