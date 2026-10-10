"""Independent Evolution page journeys on the real shell and isolated app server."""
from __future__ import annotations
import json
from pathlib import Path
import pytest

pytestmark = pytest.mark.visual


def _fixture_routes(page, *, activation_pending=False):
    goal = {'goal_id': 'goal-evidence', 'statement': '修复论文引用检查', 'domain': 'code',
            'status': 'admitted', 'exploratory': False, 'created_at': 1,
            'criteria': [{'criterion_id': 'c1', 'kind': 'test_command', 'params': {'command': 'pytest -q'}}],
            'budget_envelope': {'max_tokens': 200000, 'max_wall_clock_seconds': 7200, 'max_tool_calls': 200}}
    mutation = {'mutation_id': 'mutation-evidence', 'kind': 'skill_candidate', 'status': 'proposed',
                'version': 1, 'parent_version': 0, 'payload': {'name': '提升文献解析能力'}, 'evidence_episode_ids': ['episode-evidence']}
    episode = {'episode_id': 'episode-evidence', 'goal_id': goal['goal_id'], 'task_id': 'task-evidence',
               'status': 'success', 'actuals': {'tokens': 22000, 'wall_clock_seconds': 90, 'tool_calls': 12},
               'verdict': {'outcome': 'success', 'assessed_by': 'checker', 'criterion_results': [
                   {'criterion_id': 'c1', 'outcome': 'pass', 'evidence_ref': '12 tests passed'}]}}
    plan = {'plan_id': 'plan-evidence', 'goal_id': goal['goal_id'], 'status': 'proposed',
            'selected_path_id': 'path-measured', 'candidates': [{'path_id': 'path-measured', 'title': '使用现有测试定位并修复',
             'cost_estimate': {'expected_tokens': 24000, 'priors_source': 'measured_history'}, 'utility_over_cost': 1}]}
    if activation_pending:
        mutation.update(kind='budget_policy',status='confirmed',payload={'policy':'{}'},
            activation_intent={'started_at':2,'context_fingerprint':'0'*64})
    actions = []
    def route(request_route):
        request = request_route.request
        tail = request.url.split('/api/v4/evolution/')[1].split('?')[0]
        if request.method == 'GET':
            body = {'programs': {'programs': []}, 'runs': {'runs': [], 'proposals': {'items': [{**mutation,'title':'Pending policy','evidence_count':1,'updated_at':2}] if activation_pending and mutation['status']=='confirmed' else [], 'degraded':False}}, 'models': {'models': []}, 'goals': {'goals': [goal]}, 'plans': {'plans': [plan]}, 'episodes': {'episodes': [episode]},
                    'mutations': {'mutations': [mutation]}, 'capability-gaps': {'capability_gaps': []},
                    'skills': {'skills': []}, 'snapshot': {'snapshot': {'strategies': 1, 'anti_patterns': 0}}}.get(tail, {'report': '# Evidence\n12 tests passed'})
        elif tail.endswith('/enable'):
            actions.append('enable')
            if mutation.get('activation_intent'):
                mutation.pop('activation_intent');mutation['status']='active'
                body={'mutation':mutation,'final_status':'active'}
            else:
                mutation['status']='confirmed'
                body={'mutation':mutation,'final_status':'needs_waiver','receipt':{'verdict':'unmeasurable'}}
        elif tail.endswith('/enable_waiver'):
            actions.append('enable_waiver'); mutation['status'] = 'active'
            body = {'mutation': mutation, 'final_status': 'active', 'receipt': {'verdict': 'unmeasurable'}}
        else:
            body = {}
        request_route.fulfill(status=200, content_type='application/json', body=json.dumps(body))
    page.route('**/api/v4/evolution/**', route)
    return actions


@pytest.mark.parametrize('width', [390, 1440])
def test_workspace_routes_forms_decisions_and_chat_restoration(page, live_server, width):
    from playwright.sync_api import expect
    from tests.conftest import _dismiss_onboarding_modals
    page.set_viewport_size({'width': width, 'height': 960})
    actions = _fixture_routes(page)
    # Test initial mobile layout, rather than preserving a desktop-open drawer
    # while resizing the already booted shared page fixture.
    page.reload(wait_until='domcontentloaded')
    _dismiss_onboarding_modals(page)
    if width < 768 and page.locator('#sidebarBackdrop').is_visible():
        page.locator('#sidebarBackdrop').click(position={'x':width-2,'y':150})
    page.locator('#userInput').fill('Keep my chat draft')
    page.locator('#evolutionBtn').click()
    expect(page.locator('.exp-create-form')).to_be_visible(timeout=20000)
    expect(page.locator('.chat-wrapper')).not_to_be_visible()
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    output = Path('output/playwright'); output.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(output / f'evolution-overview-{width}.png'), full_page=True)
    page.go_back()
    expect(page.locator('#userInput')).to_be_visible()
    expect(page.locator('#userInput')).to_have_value('Keep my chat draft')
    page.go_forward()
    expect(page.locator('.evo-main')).to_be_visible()
    _dismiss_onboarding_modals(page)
    page.evaluate("location.hash='/evolution/new'")
    expect(page.locator('#evo-statement')).to_be_visible()
    page.locator('#evo-statement').fill('An unfinished goal draft')
    page.locator('#evo-statement').focus()
    page.evaluate('''() => {
      window.evolutionTestInput = document.querySelector('#evo-statement');
      document.querySelector('[data-evo-action=refresh]').click();
    }''')
    expect(page.locator('[data-evo-action=refresh]')).to_be_enabled()
    assert page.evaluate("document.querySelector('#evo-statement') === window.evolutionTestInput")
    expect(page.locator('#evo-statement')).to_have_value('An unfinished goal draft')
    page.screenshot(path=str(output / f'evolution-form-{width}.png'), full_page=True)
    page.goto(live_server + '/#/evolution/goals/goal-evidence')
    from tests.conftest import _dismiss_onboarding_modals
    _dismiss_onboarding_modals(page)
    expect(page.locator('.evo-content')).to_contain_text('12 tests passed', timeout=20000)
    page.screenshot(path=str(output / f'evolution-goal-{width}.png'), full_page=True)
    page.locator('[data-tab=capabilities]').click()
    page.locator('.evo-content a[href$="mutation-evidence"]').click()
    page.locator('[data-evo-action=enable]').click()
    expect(page.locator('[data-evo-action=waiver]')).to_be_visible()
    assert actions == ['enable']
    page.screenshot(path=str(output / f'evolution-waiver-{width}.png'), full_page=True)
    page.locator('[data-evo-action=waiver]').click()
    expect(page.locator('.evo-gate-result')).to_contain_text('已启用')
    assert actions == ['enable', 'enable_waiver']
    page.locator('.evo-nav a[data-tab=overview]').click()
    page.locator('.exp-secondary-nav a[href="#/chat"]').click()
    expect(page.locator('#userInput')).to_be_visible()
    page.locator('#paperModeBtn').click()
    expect(page.locator('#paperModeContainer')).to_be_visible(timeout=20000)
    expect(page.locator('#evolutionPageContainer')).not_to_be_visible()
    if width < 768 and page.locator('#sidebarBackdrop').is_visible():
        page.locator('#sidebarBackdrop').click(position={'x':width-2,'y':150})
    page.locator('#paperModeBtn').click()
    expect(page.locator('#userInput')).to_be_visible()
    # The reload above intentionally tests deep-link hydration. A fresh draft
    # verifies navigation restoration without depending on cross-reload storage.
    page.locator('#userInput').fill('Keep my navigation draft')
    page.locator('#evolutionBtn').click()
    expect(page.locator('.evo-main')).to_be_visible()
    page.go_back()
    expect(page.locator('#userInput')).to_be_visible()
    expect(page.locator('#userInput')).to_have_value('Keep my navigation draft')
    assert page.locator('.evo-main').count() == 0


def test_independent_design_preview(browser):
    """The exact page mounts without the chat runtime or production API graph."""
    import os
    import socket
    import subprocess
    import time
    import urllib.request
    from playwright.sync_api import expect
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    process = subprocess.Popen(['node', 'node_modules/vite/bin/vite.js', '--config', 'frontend/src/features/evolution/testing/vite.config.mjs', '--host', '127.0.0.1',
                                '--port', str(port), '--strictPort'], stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, start_new_session=True)
    context = browser.new_context(viewport={'width': 1280, 'height': 900})
    try:
        base = f'http://127.0.0.1:{port}'
        for _ in range(100):
            if process.poll() is not None:
                raise RuntimeError('Preview server exited before readiness')
            try:
                urllib.request.urlopen(base + '/preview.html', timeout=.2).close()
                break
            except OSError:
                time.sleep(.1)
        page = context.new_page()
        errors = []
        api_requests = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        # Vite serves frontend API *modules* under /@fs/.../src/api/; these
        # source reads are distinct from a request to the application API.
        from urllib.parse import urlsplit
        page.on('request', lambda request: api_requests.append(request.url)
                if urlsplit(request.url).path.startswith('/api/') else None)
        page.goto(base + '/preview.html#/evolution/start')
        expect(page.locator('#evo-objective')).to_be_visible(timeout=20000)
        assert page.evaluate('typeof window.TofuModules') == 'undefined'
        page.keyboard.press('Escape')
        page.goto(base + '/preview.html?scenario=empty#/evolution/start')
        expect(page.locator('#evo-objective')).to_be_visible()
        page.keyboard.press('Escape')
        page.goto(base + '/preview.html?scenario=failure#/evolution/start')
        expect(page.locator('.evo-error')).to_contain_text('模拟连接中断')
        page.keyboard.press('Escape')
        page.goto(base + '/preview.html#/evolution/start')
        page.keyboard.press('Escape')
        expect(page.locator('#preview-theme, #preview-state, .evo-preview-tools')).to_have_count(0)
        page.goto(base + '/preview.html?scenario=empty#/evolution/start')
        expect(page.locator('#evo-objective')).to_be_visible()
        Path('output/playwright').mkdir(parents=True, exist_ok=True)
        page.screenshot(path='output/playwright/evolution-preview-clean.png', full_page=True)
        assert not api_requests
        assert not errors
    finally:
        context.close()
        import signal
        os.killpg(process.pid, signal.SIGTERM)
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=5)


@pytest.mark.parametrize('width',[390,1440])
@pytest.mark.parametrize('surface',['detail','studio'])
def test_pending_policy_activation_has_clear_continuation_without_rejection(page,live_server,tmp_path,width,surface):
    from playwright.sync_api import expect
    from tests.conftest import _dismiss_onboarding_modals
    page.set_viewport_size({'width':width,'height':960})
    actions=_fixture_routes(page,activation_pending=True)
    target='capabilities/mutation-evidence' if surface=='detail' else 'runs'
    page.goto(live_server+'/#/evolution/'+target)
    _dismiss_onboarding_modals(page)
    if surface=='studio':
        page.locator('wa-button[data-studio-action=menu]').click()
        page.locator('[data-studio-action=proposals]').click()
        panel=page.locator('.evo-proposals')
        continuation=panel.locator('[data-studio-action=proposal-enable]')
        reject='[data-studio-action=proposal-reject]'
    else:
        panel=page.locator('.evo-content')
        continuation=panel.locator('[data-evo-action=enable]')
        reject='[data-evo-action=rejectMutation]'
    expect(panel).to_contain_text('启用进行中',timeout=20000)
    expect(continuation).to_have_text('继续启用')
    assert panel.locator(reject).count()==0
    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
    page.screenshot(path=str(tmp_path/f'activation-pending-{surface}-{width}.png'),full_page=True)
    continuation.focus();continuation.press('Enter')
    expect(panel).to_contain_text('已启用',timeout=15000)
    assert actions==['enable']
    assert panel.locator(reject).count()==0
