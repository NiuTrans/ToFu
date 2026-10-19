"""Real app + isolated storage: project leader creation is not ordinary pinning.

Only the read-only work board is controlled; create/bind/navigation use real
HTTP handlers. No model or background agent is invoked.
"""
from pathlib import Path
import uuid

import pytest
from playwright.sync_api import expect

pytestmark = pytest.mark.visual


def test_create_independent_leader_and_keep_project_navigation_isolated(page, monkeypatch, tmp_path, assert_no_js_errors):
    from tests._seed import seed_conversation
    from tests.conftest import _dismiss_onboarding_modals
    import routes.api_v1.folders as folders

    monkeypatch.setattr(folders, '_FOLDERS_PATH', str(tmp_path / 'folders.json'))
    origin = page.url.split('/?', 1)[0].rstrip('/')
    suffix = uuid.uuid4().hex
    roots, folder_ids = [], []
    for name in ['Alpha', 'Beta']:
        root = tmp_path / name
        root.mkdir()
        roots.append(str(root))
        response = page.request.post(origin + '/api/v4/folders', data={
            'name': name, 'projectPaths': [str(root)],
        })
        assert response.ok, response.text()
        folder_ids.append(response.json()['data']['id'])
    ordinary, worker = ['leader-journey-' + kind + '-' + suffix for kind in ['ordinary', 'worker']]
    build_worker, review_worker = ['leader-journey-' + kind + '-' + suffix for kind in ['build', 'review']]
    for cid, title in [(ordinary, 'Ordinary discussion'), (worker, 'Research task'),
                       (build_worker, 'Implementation task'), (review_worker, 'Validation task')]:
        seed_conversation(cid, title=title,
                          settings={'folderId': folder_ids[0], 'projectPath': roots[0]},
                          messages=[{'role': 'user', 'content': title + ' transcript'}])
    page.route('**/api/v4/project/board?*', lambda route: route.fulfill(json={'meta': {}, 'data': {
        'active': [{'id': 'research', 'conversationId': worker, 'title': 'Research task',
                    'status': 'active', 'resultSummary': 'Checking primary sources'}],
        'recentOutcomes': [],
    }}))
    assignments = []
    page.route('**/api/v4/project/coordination/assignments?*',
               lambda route: route.fulfill(json={'meta': {}, 'data': {'assignments': assignments}}))
    page.evaluate('localStorage.setItem("tofu_ui_lang", "en")')
    page.reload()
    _dismiss_onboarding_modals(page)
    page.set_viewport_size({'width': 1440, 'height': 960})
    page.locator(f'.folder-tab[data-folder-id="{folder_ids[0]}"]').click()
    page.locator(f'.conv-item[data-conv-id="{ordinary}"] .conv-title').click(position={'x': 4, 'y': 4})
    page.wait_for_function('(id) => window.activeConvId === id', arg=ordinary)
    entry = page.locator('#projectCoordinatorEntry')
    entry.get_by_role('button', name='Create project Coordinator', exact=True).click()
    pinned = entry.locator('.coordination-leader')
    page.wait_for_function('document.querySelector("#projectCoordinatorEntry .coordination-leader")?.dataset.coordinatorId')
    leader = pinned.get_attribute('data-coordinator-id')
    assert leader and leader not in [ordinary, worker]
    page.wait_for_function('(id) => window.activeConvId === id', arg=leader)
    binding = page.request.get(origin + '/api/v4/project/coordination', params={'path': roots[0]})
    assert binding.ok, binding.text()
    assert binding.json()['data']['conversationId'] == leader
    # Ordinary accepted Turn + physical account receipts, read through the real
    # budget API. No provider or Worker is launched by this browser fixture.
    from lib import turn_lifecycle as turns
    from lib.identity import PrincipalContext
    from lib.project_dispatch.request_budget import bind_source_account
    pair = turns.create_turn_pair(leader, user_id=1, command_id='browser-budget-' + suffix,
        input_projection={'content': 'Implement and verify this project'}, config={'projectPath': roots[0]})
    source_task_id = 'browser-coordinator-budget-' + suffix
    turns.bind_task(pair['attempt']['attemptId'], source_task_id, user_id=1)
    turns.mark_task_started(pair['attempt']['attemptId'], source_task_id, user_id=1)
    budget_task = {'id': source_task_id, 'convId': leader, '_userId': 1, '_attended': True, 'status': 'running',
        '_turnId': pair['attempt']['turnId'], '_attemptId': pair['attempt']['attemptId'],
        '_principalContext': PrincipalContext.user(subject_id='user:1', owner_user_id=1, scopes=[]).to_payload(),
        'config': {'projectPath': roots[0]}}
    bind_source_account(budget_task)
    lease = budget_task['_executionBudget'].reserve_model(budget_task, input_tokens=50, max_output_tokens=50)
    lease.mark_dispatched(); lease.settle({'prompt_tokens': 50, 'completion_tokens': 25})
    assert page.locator(f'#convList .conv-item[data-conv-id="{leader}"]').count() == 0
    assert pinned.evaluate('(el) => !!(el.compareDocumentPosition(document.querySelector("#convList")) & Node.DOCUMENT_POSITION_FOLLOWING)')
    assert page.locator('[data-testid="collab-bar"]').count() == 0

    def assignment(aid, recipient, brief, status, dependencies=None):
        dependencies = dependencies or []
        return {'assignmentId': aid, 'grantRevision': 1, 'revoked': False, 'canonicalDelivery': False,
                'request': {'projectPath': roots[0], 'coordinatorId': leader, 'recipientId': recipient,
                            'brief': brief, 'dependencies': dependencies},
                'dependencyStates': [{'reference': ref, 'status': 'ready', 'code': 'accepted',
                                      'executionId': ref['assignmentId']} for ref in dependencies],
                'execution': {'status': status, 'resultRevision': 1}, 'review': None}
    assignments.extend([
        assignment('research', worker, 'Research task', 'running'),
        assignment('implementation', build_worker, 'Implementation task', 'queued',
                   [{'assignmentId': 'design', 'grantRevision': 1, 'resultRevision': 1, 'predicate': 'accepted'}]),
        assignment('validation', review_worker, 'Validation task', 'failed'),
        assignment('design', worker, 'Requirements and design', 'completed'),
    ])
    control = assignment('control', leader, 'Internal continuation of the original request', 'completed')
    control['request']['profile'] = {'mode': 'coordinator', 'tools': ['get_project_workers'],
                                     'readPaths': [], 'writePaths': [], 'maxToolCalls': 512}
    assignments.append(control)
    # Re-enter through the real sidebar so fresh recorded assignments drive the
    # chat graph; no test-only refresh or alternate conversation renderer.
    page.locator(f'.conv-item[data-conv-id="{ordinary}"] .conv-title').click(position={'x': 4, 'y': 4})
    page.wait_for_function('(id) => window.activeConvId === id', arg=ordinary)
    # A real report reaches the sidebar by project push while another chat is
    # open. Fetching/rendering it cannot mark it read or trigger model work.
    from lib.conversations.coordinator_reports import CoordinatorReports
    reports = CoordinatorReports(1, roots[0])
    scope = {'owner': 1, 'projectPath': roots[0], 'coordinatorId': leader,
             'bindingRevision': binding.json()['data']['revision']}
    first_report = reports.publish(scope, {
        'title': 'Migration window needs your decision',
        'message': 'The implementation and checks are ready. Choose a maintenance window before publication.',
        'kind': 'decision', 'idempotency_key': 'browser-decision',
    })
    page.locator('#projectCoordinatorEntry .coordination-leader[data-unread="true"]').wait_for()
    assert reports.read()['unread'] is True
    entry.locator('.coordination-leader').click()
    page.wait_for_function('(id) => window.activeConvId === id', arg=leader)
    summary = page.locator('#presenceStrip .coordination-summary')
    summary.wait_for(state='visible')
    assert summary.get_attribute('open') is not None
    assert summary.locator('.coordination-task-graph').count() == 1
    assert page.locator('dialog[open]').count() == 0
    # A completed prerequisite stays in the graph of current work.
    assert summary.locator('.coordination-graph-node[data-node-kind="task"]').count() == 4
    assert summary.locator('svg [data-edge-kind="dependency"]').count() == 3
    assert summary.locator('.coordination-group').count() == 0
    assert 'Internal continuation' not in summary.inner_text()
    assert '75 used / 2,000,000' in summary.locator('.coordinator-budget').inner_text()
    summary.get_by_role('button', name='Manage work', exact=True).click()
    workspace = page.locator('dialog[open]')
    workspace.get_by_role('tab', name='Manage work', exact=True).click()
    workspace.locator('.coordinator-budget-settings').get_by_role('spinbutton', name='Tokens').fill('500000')
    workspace.get_by_role('button', name='Save future budget', exact=True).click()
    expect(workspace.locator('.coordinator-budget-settings')).to_contain_text('Saved. Future requests will use this budget.')
    saved_budget = page.request.get(origin + '/api/v4/project/coordination/budget', params={'path': roots[0]}).json()['data']
    assert saved_budget['settings']['maxTokens'] == 500000
    assert saved_budget['request']['policy']['maxTokens'] == 2000000
    workspace.get_by_role('button', name='Back to conversation', exact=True).click()
    expect(workspace).to_have_count(0)
    report = page.locator('#presenceStrip .coordination-report')
    report.locator('summary').click()
    assert 'Choose a maintenance window' in report.inner_text()
    assert reports.read()['unread'] is True
    assert 'Migration window needs your decision' in page.locator('#chatInner').inner_text()
    impl = summary.locator('[data-node-id="assignment:implementation"] button')
    impl.click()
    assert 'Requirements and design' in summary.locator('.coordination-node-inspector').inner_text()
    for theme, width in [('tofu', 1440), ('dark', 390), ('light', 320)]:
        page.evaluate('(theme) => document.documentElement.dataset.theme = theme', theme)
        page.set_viewport_size({'width': width, 'height': 960})
        if width < 600:
            page.evaluate('() => { if (!document.getElementById("sidebar").classList.contains("collapsed")) window.toggleSidebar(); }')

            page.wait_for_function('document.getElementById("sidebar").getBoundingClientRect().right <= 1')
        assert summary.evaluate('(el) => el.scrollWidth <= el.clientWidth + 1')
        output = Path(__file__).resolve().parents[2] / 'output/playwright/project-coordinator'
        output.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(output / f'{theme}-{width}.png'), animations='disabled')
        budget_card = summary.locator('.coordinator-budget')
        assert budget_card.evaluate('(el) => el.scrollWidth <= el.clientWidth + 1')
        budget_card.screenshot(path=str(output / f'budget-{theme}-{width}.png'), animations='disabled')
    page.set_viewport_size({'width': 1440, 'height': 960})
    page.evaluate('() => { if (document.getElementById("sidebar").classList.contains("collapsed")) window.toggleSidebar(); }')
    report.get_by_role('button', name='Mark this message as read', exact=True).click()
    page.locator('#projectCoordinatorEntry .coordination-leader:not([data-unread="true"])').wait_for()
    assert reports.read()['unread'] is False
    page.reload()
    _dismiss_onboarding_modals(page)
    page.wait_for_function('(id) => window.activeConvId === id', arg=leader)
    page.locator('#projectCoordinatorEntry .coordination-leader:not([data-unread="true"])').wait_for()
    assert reports.read()['latest']['turnId'] == first_report['turnId']
    reports.publish(scope, {'title': 'Delivery verified', 'message': 'All acceptance checks passed.',
                           'kind': 'delivery', 'idempotency_key': 'browser-delivery'})
    page.locator('#projectCoordinatorEntry .coordination-leader[data-unread="true"]').wait_for()
    assert 'Delivery verified' in entry.inner_text()
    assert reports.read()['unread'] is True
    summary.locator('[data-node-id="assignment:research"] button').click()
    summary.get_by_role('button', name='Open conversation: Research task', exact=True).click()
    page.wait_for_function('(id) => window.activeConvId === id', arg=worker)

    # The primary sidebar entry must navigate to chat, never a management modal.
    entry.locator('.coordination-leader').click()
    page.wait_for_function('(id) => window.activeConvId === id', arg=leader)
    assert page.locator('dialog[open]').count() == 0
    research_node = page.locator('#presenceStrip [data-node-id="assignment:research"] button')
    if research_node.get_attribute('aria-pressed') != 'true':
        research_node.click()
    page.locator('#presenceStrip').get_by_role('button', name='Open conversation: Research task', exact=True).click()
    page.wait_for_function('(id) => window.activeConvId === id', arg=worker)
    page.locator('#presenceStrip').get_by_role('button', name='Return to Coordinator', exact=True).click()
    page.wait_for_function('(id) => window.activeConvId === id', arg=leader)
    page.locator(f'.folder-tab[data-folder-id="{folder_ids[1]}"]').click()
    entry.get_by_role('button', name='Create project Coordinator', exact=True).wait_for(state='visible')
    # Browsing Beta must not rewrite the active Alpha conversation's ownership.
    page.locator(f'.folder-tab[data-folder-id="{folder_ids[0]}"]').click()
    entry.locator('.coordination-leader').wait_for(state='visible')
    assert entry.locator('.coordination-leader').get_attribute('data-coordinator-id') == leader
    page.locator(f'.conv-item[data-conv-id="{ordinary}"] .conv-title').click(position={'x': 4, 'y': 4})
    page.wait_for_function('(id) => window.activeConvId === id', arg=ordinary)
    page.wait_for_function('!document.querySelector(".coordination-summary")')
    expect(page.locator('#chatInner')).to_contain_text('Ordinary discussion transcript')
    assert page.locator('#presenceStrip').get_by_role('button', name='Return to Coordinator', exact=True).count() == 0
