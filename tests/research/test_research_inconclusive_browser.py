"""Real browser separates task completion from scientific acceptance.

Only the production-task starter is controlled; workspace CAS, task reads and
browser polling/rendering are real. No provider or external experiment executes.
"""
import uuid
import pytest
from tests.research.test_research_workspace import storage as storage
from tests.e2e.test_e2e_smoke import _wait_app_ready, _SENTINEL
from tests.e2e.test_e2e_smoke import _install_llm_stubs, _disable_open_mode_rate_limit  # noqa: F401
from lib.research.records import persist_record
from lib.research.workspace import empty_workspace, save_workspace, load_workspace
from lib.research.program import readiness

pytestmark = pytest.mark.visual


def test_done_action_keeps_inconclusive_decision_and_submission_blocked(
        page, storage, monkeypatch, assert_no_js_errors, tmp_path):
    import lib.research.capabilities as capabilities
    import routes.api_v1.tasks as task_routes
    from lib.research.action_runtime import create_action_task, _research_action_runtime
    direction = 'Controlled inconclusive browser ' + uuid.uuid4().hex[:8]
    page.add_init_script("if (location.protocol.startsWith('http')) localStorage.setItem('tofu_ui_lang', 'en');")
    page.reload(); _wait_app_ready(page)
    monkeypatch.setattr(capabilities, 'build_capability_catalog', lambda **kwargs: {
        'contract_version': 'tofu.research-capabilities/v1', 'capabilities': [], 'tools': [], 'fingerprint': ''})
    draft = empty_workspace(direction)
    draft['hypothesis'] = 'A controlled UI example, not a measured scientific claim.'
    draft['protocol'].update(dataset='controlled', primary_metric='accuracy', baseline='reference',
        random_seeds=[1, 2], decision_rule={'direction':'higher','minimum_effect':0,'uncertainty_multiplier':2})
    save_workspace(direction, 'en', draft, expected_revision=0, user_id=1)
    persist_record({'task_id': 'record-' + uuid.uuid4().hex, 'user_id': 1, 'direction': direction,
        'lang': 'en', 'status': 'done', 'result': {'survey_md': 'Controlled discovery artifact.', 'accepted': [], 'rejected': []}})
    starters = task_routes._starters(); spec = dict(starters['research-action'])
    created = []
    def start(direction, *, action, expected_revision, user_id, **kwargs):
        assert action == 'experiment' and kwargs['confirm_external_writes'] is True
        current = load_workspace(direction, 'en', user_id=user_id)
        assert current['revision'] == expected_revision
        current['runs'] = [{'id':'inconclusive-run','status':'inconclusive',
            'execution_status':'completed','scientific_outcome':'inconclusive',
            'notes':'No validated measurement receipt; no scientific support.'}]
        saved = save_workspace(direction, 'en', current, expected_revision=expected_revision, user_id=user_id)
        task_id = 'research_action_' + uuid.uuid4().hex[:16]
        task = create_action_task(task_id, user_id=user_id,
            fields={'direction':direction,'action':action,'lang':'en'})
        # Controlled terminal fixture; no task execution or persistence claim.
        task.update(status='done', result={'workspace':saved,'readiness':readiness(saved),
            'summary':'Task ended; scientific evidence remains inconclusive.'})
        created.append((task_id, user_id))
        return {'task_id':task_id,'deduped':False}
    spec['start'] = start
    monkeypatch.setattr(task_routes, '_starters', lambda: {**starters, 'research-action':spec})
    try:
        page.evaluate("window.location.hash='/evolution/research'")
        page.locator('.research-record-item').filter(has_text=direction).click()
        page.locator('[data-rs-tab="workspace"]').click()
        calls = _SENTINEL['stream_calls']
        requests = []
        page.on('request', lambda request: requests.append(request.post_data_json)
            if request.url.endswith('/api/v4/tasks/start') and request.method == 'POST' else None)
        page.on('dialog', lambda dialog: dialog.accept())
        page.locator('[data-tofu-action="_startResearchAction(\'experiment\',this)"]').click()
        notes = page.locator('[data-rsw-field="runs.0.notes"]')
        page.wait_for_function("document.querySelector('[data-rsw-field=\"runs.0.notes\"]')?.value.includes('No validated measurement')")
        assert notes.is_visible()
        assert page.locator('#researchWorkspace .rsw-record strong').filter(has_text='inconclusive').is_visible()
        assert not page.locator('#researchWorkspace .rsw-gates.is-ready').count()
        page.screenshot(path=str(tmp_path/'research-inconclusive-decision.png'), full_page=True)
        page.locator('[data-rs-tab="paper"]').click()
        gate = page.locator('[data-gate-id="run_evidence"]')
        gate.locator('xpath=ancestor::details').evaluate('(element)=>element.open=true')
        assert gate.is_visible() and 'is-ok' not in (gate.get_attribute('class') or '')
        assert _SENTINEL['stream_calls'] == calls and len(created) == 1
        assert len(requests) == 1 and isinstance(requests[0].get('model'), str)
        assert requests[0]['model'].strip()
        page.screenshot(path=str(tmp_path/'research-done-inconclusive.png'), full_page=True)
    finally:
        for task_id, owner in created: _research_action_runtime.remove_owned(task_id, user_id=owner)
