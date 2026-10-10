"""Real-server Chromium inspects retained observations without changing drafts."""
import pytest
import re
from tests.research.test_research_workspace import storage as storage
from tests.research.test_research_history import draft_with_runs
from lib.research.workspace import save_workspace

DIRECTION = 'Retained experiment observation browser recovery'
from tests.e2e.test_e2e_smoke import _wait_app_ready, _SENTINEL
from tests.e2e.test_e2e_smoke import _install_llm_stubs, _disable_open_mode_rate_limit  # noqa: F401
from lib.research.records import persist_record

pytestmark = pytest.mark.visual


def test_retained_observations_page_without_model_or_draft_mutation(page, storage, monkeypatch,
        assert_no_js_errors, tmp_path):
    import lib.research.capabilities as capabilities
    from lib.research.action import capture_research_result
    from lib.research.action_observations import capture_action_observation
    page.add_init_script("if (location.protocol.startsWith('http')) localStorage.setItem('tofu_ui_lang', 'en');")
    page.reload()
    _wait_app_ready(page)
    monkeypatch.setattr(capabilities, 'build_capability_catalog', lambda **_: {
        'contract_version': 'tofu.research-capabilities/v1', 'capabilities': [], 'tools': [], 'fingerprint': ''})
    draft = draft_with_runs(0); draft['direction'] = DIRECTION
    saved = save_workspace(DIRECTION, 'en', draft, user_id=1, expected_revision=0)
    task_id = 'retained-browser-experiment'
    task = {'user_id': 1, 'task_id': task_id, 'direction': DIRECTION}
    for ordinal in range(1, 10):
        row = {'roundNum': ordinal, 'toolName': 'run_command', 'status': 'done', 'toolCallId': str(ordinal)}
        content = f'Observed experiment result {ordinal}: <script>must remain text</script>'
        capture_research_result(row, content)
        capture_action_observation(task, saved, row, content)
    persist_record({'task_id': 'observation-browser-research', 'user_id': 1,
        'direction': DIRECTION, 'lang': 'en', 'status': 'done',
        'result': {'survey_md': 'Retained observation recovery.', 'accepted': [], 'rejected': []}})
    page.evaluate("window.location.hash = '/evolution/research'")
    page.locator('.research-record-item').filter(has_text=DIRECTION).click()
    page.locator('[data-rs-tab="workspace"]').click()
    host = page.locator('#researchObservationRecovery')
    host.wait_for()
    editor = page.locator('#researchWorkspace [data-rsw-field="hypothesis"]')
    editor.fill('Unsaved research hypothesis')
    calls = _SENTINEL['stream_calls']
    host.locator('input').fill(task_id)
    host.get_by_role('button', name=re.compile('^(Inspect observations|查看保留观察)$')).click()
    host.locator('article').first.wait_for()
    assert host.locator('article').count() == 8
    host.locator('summary').first.click()
    assert '<script>must remain text</script>' in host.locator('pre').first.inner_text()
    assert host.locator('script').count() == 0
    host.get_by_role('button', name=re.compile('^(Next observations|下一页观察)$')).click()
    page.wait_for_function("document.querySelectorAll('#researchObservationRecovery article').length === 1")
    assert '9: run_command' in host.inner_text()
    assert editor.input_value() == 'Unsaved research hypothesis'
    assert _SENTINEL['stream_calls'] == calls
    host.locator('summary').first.click()
    page.screenshot(path=str(tmp_path / 'research-observation-recovery-desktop.png'))
    page.set_viewport_size({'width': 390, 'height': 844})
    host.scroll_into_view_if_needed()
    assert host.evaluate('(el) => el.scrollWidth <= el.clientWidth + 1')
    page.screenshot(path=str(tmp_path / 'research-observation-recovery-mobile.png'))
    host.locator('input').fill('unknown-experiment')
    host.get_by_role('button', name=re.compile('^(Inspect observations|查看保留观察)$')).click()
    host.get_by_text(re.compile('No retained observations were found for this task.|未找到此任务保留的观察。')).wait_for()
