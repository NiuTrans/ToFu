"""Real app + isolated storage: the session-free Coordinator chat.

The leader journey uses real create/bind/navigation handlers, the real task
board store and report publication; the chat shows only the board. Manage work
reads real server history rows. No model or background agent is invoked.
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
    ordinary = 'leader-journey-ordinary-' + suffix
    seed_conversation(ordinary, title='Ordinary discussion',
                      settings={'folderId': folder_ids[0], 'projectPath': roots[0]},
                      messages=[{'role': 'user', 'content': 'Ordinary discussion transcript'}])
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
    assert leader and leader != ordinary
    page.wait_for_function('(id) => window.activeConvId === id', arg=leader)
    binding = page.request.get(origin + '/api/v4/project/coordination', params={'path': roots[0]})
    assert binding.ok, binding.text()
    assert binding.json()['data']['conversationId'] == leader
    assert page.locator(f'#convList .conv-item[data-conv-id="{leader}"]').count() == 0
    assert pinned.evaluate('(el) => !!(el.compareDocumentPosition(document.querySelector("#convList")) & Node.DOCUMENT_POSITION_FOLLOWING)')
    assert page.locator('[data-testid="collab-bar"]').count() == 0
    # Before the Coordinator writes a board, the chat says so instead of showing sessions.
    board = page.locator('#presenceStrip .coordinator-progress')
    expect(board).to_have_attribute('data-state', 'empty')
    # A Worker the Coordinator created is a session the human never has to track.
    worker = 'leader-journey-worker-' + suffix
    seed_conversation(worker, title='Research task',
                      settings={'folderId': folder_ids[0], 'projectPath': roots[0], '_coordinatorSource': leader},
                      messages=[{'role': 'user', 'content': 'Research task transcript'}])

    # The Coordinator's own board (real store + HTTP), written while another chat is open.
    from lib.conversations.coordinator_progress import CoordinatorTaskProgress
    from lib.conversations.coordinator_reports import CoordinatorReports
    scope = {'owner': 1, 'projectPath': roots[0], 'coordinatorId': leader,
             'bindingRevision': binding.json()['data']['revision']}
    page.locator(f'.conv-item[data-conv-id="{ordinary}"] .conv-title').click(position={'x': 4, 'y': 4})
    page.wait_for_function('(id) => window.activeConvId === id', arg=ordinary)
    CoordinatorTaskProgress(1, roots[0]).publish(scope, {
        'summary': 'The folder dialog fix is verified; homestay research is underway.',
        'tasks': [
            {'title': 'Fix folder creation in the sidebar', 'status': 'completed', 'progress': 'Merged and verified in the browser.'},
            {'title': 'Research New Year homestays in Tianyu Creek', 'status': 'in_progress',
             'progress': 'Comparing Xiaohongshu, Ctrip, YourProvider and Douyin listings.'},
            {'title': 'Summarize the WeChat article', 'status': 'pending'},
        ]})
    page.wait_for_function('document.querySelector("#projectCoordinatorEntry .coordination-leader-meta")?.textContent === "In progress: 1"')
    # A real report reaches the sidebar by project push while another chat is open.
    reports = CoordinatorReports(1, roots[0])
    first_report = reports.publish(scope, {
        'title': 'Folder fix delivered', 'message': 'The sidebar folder dialog now creates and deletes folders.',
        'kind': 'delivery', 'idempotency_key': 'browser-delivery-1',
    })
    page.locator('#projectCoordinatorEntry .coordination-leader[data-unread="true"]').wait_for()
    assert reports.read()['unread'] is True
    assert page.locator('#presenceStrip .coordinator-progress').count() == 0

    # Opening the visible Coordinator chat reads its latest message, like any messaging app.
    entry.locator('.coordination-leader').click()
    page.wait_for_function('(id) => window.activeConvId === id', arg=leader)
    expect(board).to_have_attribute('data-state', 'board')
    assert board.get_attribute('open') is not None
    page.locator('#projectCoordinatorEntry .coordination-leader:not([data-unread="true"])').wait_for()
    assert reports.read()['unread'] is False and reports.read()['latest']['turnId'] == first_report['turnId']
    expect(board.locator('.coordinator-progress-summary')).to_have_text(
        'The folder dialog fix is verified; homestay research is underway.')
    assert board.locator('.coordinator-progress-body > .coordinator-progress-tasks > li').evaluate_all(
        '(rows) => rows.map((row) => [row.dataset.status, row.querySelector("strong").textContent])') == [
        ['in_progress', 'Research New Year homestays in Tianyu Creek'], ['pending', 'Summarize the WeChat article']]
    expect(board.locator('.coordinator-progress-finished > summary')).to_have_text('Finished: 1')
    # Session-free: no sessions, references, graph, usage or report banner in the chat.
    strip = page.locator('#presenceStrip')
    for retired in ['.coordination-work-row', '.coordination-task-graph', '.coordination-reference',
                    '.coordinator-budget', '.coordination-report', '.coordinator-plan']:
        assert strip.locator(retired).count() == 0, retired
    assert 'Folder fix delivered' in page.locator('#chatInner').inner_text()
    # Usage and Worker records remain one explicit click away, never in the chat.
    board.get_by_role('button', name='Manage work', exact=True).click()
    workspace = page.locator('dialog[open]')
    expect(workspace.locator('.coordinator-budget')).to_contain_text('Current request usage')
    workspace.get_by_role('button', name='Back to conversation', exact=True).click()
    expect(workspace).to_have_count(0)
    for theme, width in [('tofu', 1440), ('dark', 390), ('light', 320)]:
        page.evaluate('(theme) => document.documentElement.dataset.theme = theme', theme)
        page.set_viewport_size({'width': width, 'height': 960})
        if width < 600:
            page.evaluate('() => { if (!document.getElementById("sidebar").classList.contains("collapsed")) window.toggleSidebar(); }')
            page.wait_for_function('document.getElementById("sidebar").getBoundingClientRect().right <= 1')
        assert board.evaluate('(el) => el.scrollWidth <= el.clientWidth + 1')
        output = Path(__file__).resolve().parents[2] / 'output/playwright/project-coordinator'
        output.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(output / f'{theme}-{width}.png'), animations='disabled')
        board.screenshot(path=str(output / f'board-{theme}-{width}.png'), animations='disabled')
    page.set_viewport_size({'width': 1440, 'height': 960})
    page.evaluate('() => { if (document.getElementById("sidebar").classList.contains("collapsed")) window.toggleSidebar(); }')
    # A message arriving while the Coordinator chat is open is read on arrival.
    second = reports.publish(scope, {'title': 'Research shortlist ready', 'message': 'Four homestays compared.',
                                     'kind': 'progress', 'idempotency_key': 'browser-progress-2'})
    page.wait_for_function('(text) => document.querySelector("#chatInner")?.innerText.includes(text)',
                           arg='Research shortlist ready')
    # The read command is asynchronous to the paint; poll the durable pointer it moves.
    for _ in range(50):
        state = reports.read()
        if state['latest']['turnId'] == second['turnId'] and state['unread'] is False:
            break
        page.wait_for_timeout(100)
    assert reports.read()['unread'] is False and reports.read()['latest']['turnId'] == second['turnId']
    # A non-blocking question is an answerable chat card that never expires.
    from lib import turn_lifecycle as turns
    question = 'Which nightly budget should the shortlist use?'
    turns.append_settled_turn(leader, command_id='browser-ask-' + suffix, actor='assistant', user_id=1, projection={
        'content': 'I will keep researching with under 500 RMB unless you say otherwise.',
        'segments': [
            {'type': 'tool_use', 'blockId': 'tool:call-ask', 'id': 'call-ask', 'name': 'ask_human',
             'input': {'question': question, 'response_type': 'choice', 'blocking': False},
             'result': {'status': 'done', 'content': 'Question shown to the user without waiting.'}},
            {'type': 'text', 'blockId': 'text:terminal',
             'text': 'I will keep researching with under 500 RMB unless you say otherwise.'},
        ],
        'toolRounds': [{'roundNum': 1, 'toolCallId': 'call-ask', 'toolName': 'ask_human', 'status': 'done',
                        'guidanceId': 'hg_browser_' + suffix[:8], 'guidanceQuestion': question,
                        'guidanceType': 'choice', 'guidanceBlocking': False,
                        'guidanceOptions': [{'label': 'Under 500 RMB'}, {'label': '500 to 1000 RMB'}]}],
    })
    page.reload()
    _dismiss_onboarding_modals(page)
    page.wait_for_function('(id) => window.activeConvId === id', arg=leader)
    expect(board).to_have_attribute('data-state', 'board')
    # The settled turn's choice question renders natively and stays answerable.
    card = page.locator('#chatInner [data-block-id="tool:call-ask"] .rich-choices')
    expect(card).to_have_count(1)
    status = card.locator('[role=status]')
    expect(status).to_have_text('Work continues meanwhile; your reply is sent as a normal message.')
    expect(card.locator('fieldset')).to_be_enabled()
    # With an unsent draft, answering joins the draft as a quoted reply instead of sending.
    page.locator('#userInput').fill('Also check parking.')
    card.get_by_label('Under 500 RMB').check()
    card.locator('[data-rich-control="choices-submit"]').click()
    expect(page.locator('#userInput')).to_have_value('Also check parking.\n\nUnder 500 RMB')
    expect(page.locator('#reply-quote-container')).to_contain_text(question)
    expect(status).to_have_text('Your reply is in the composer — send it when ready')
    expect(card.locator('fieldset')).to_be_enabled()
    page.locator('#userInput').fill('')

    # A Worker chat keeps exactly one way back; the sidebar entry navigates to chat.
    # (The reload leaves no folder tab selected, so open it the way navigation does.)
    page.evaluate('(id) => loadConversation(id)', worker)
    page.wait_for_function('(id) => window.activeConvId === id', arg=worker)
    assert page.locator('#presenceStrip .coordinator-progress').count() == 0
    page.locator('#presenceStrip').get_by_role('button', name='Return to Coordinator', exact=True).click()
    page.wait_for_function('(id) => window.activeConvId === id', arg=leader)
    assert page.locator('dialog[open]').count() == 0
    # Browsing Beta must not rewrite the active Alpha conversation's ownership.
    # A folder click must paint the entry inside its own handler: the list may
    # not sit empty (or jump) until the project binding read lands.
    unbound_row = page.evaluate('''(id) => {
        document.querySelector(`.folder-tab[data-folder-id="${id}"]`).click();
        const row = document.querySelector('#projectCoordinatorEntry .coordination-leader');
        return row ? { busy: row.getAttribute('aria-busy'), coordinator: row.dataset.coordinatorId } : null;
    }''', folder_ids[1])
    assert unbound_row == {'busy': 'true', 'coordinator': ''}
    entry.get_by_role('button', name='Create project Coordinator', exact=True).wait_for(state='visible')
    bound_row = page.evaluate('''(id) => {
        document.querySelector(`.folder-tab[data-folder-id="${id}"]`).click();
        const row = document.querySelector('#projectCoordinatorEntry .coordination-leader');
        return row ? { busy: row.getAttribute('aria-busy'), coordinator: row.dataset.coordinatorId } : null;
    }''', folder_ids[0])
    assert bound_row == {'busy': 'true', 'coordinator': leader}
    entry.locator('.coordination-leader').wait_for(state='visible')
    page.wait_for_function('(leader) => document.querySelector("#projectCoordinatorEntry .coordination-leader")?.dataset.coordinatorId === leader', arg=leader)
    page.locator(f'.conv-item[data-conv-id="{ordinary}"] .conv-title').click(position={'x': 4, 'y': 4})
    page.wait_for_function('(id) => window.activeConvId === id', arg=ordinary)
    page.wait_for_function('!document.querySelector(".coordinator-progress")')
    expect(page.locator('#chatInner')).to_contain_text('Ordinary discussion transcript')
    assert page.locator('#presenceStrip').get_by_role('button', name='Return to Coordinator', exact=True).count() == 0


def test_coordinator_conversations_are_contained_in_a_collapsible_sidebar_group(page, monkeypatch, tmp_path, assert_no_js_errors):
    from lib import turn_lifecycle as turns
    from lib.conversations.project_identity import normalize_project_path
    from lib.identity import PrincipalContext
    from lib.project_dispatch.repository import ProjectDispatchRepository
    from tests._seed import seed_conversation
    from tests.conftest import _dismiss_onboarding_modals
    import routes.api_v1.folders as folders

    monkeypatch.setattr(folders, '_FOLDERS_PATH', str(tmp_path / 'folders.json'))
    origin = page.url.split('/?', 1)[0].rstrip('/')
    suffix = uuid.uuid4().hex
    (tmp_path / 'Sidebar').mkdir()
    project = normalize_project_path(str(tmp_path / 'Sidebar'))
    folder = page.request.post(origin + '/api/v4/folders', data={'name': 'Sidebar', 'projectPaths': [project]})
    folder_id = folder.json()['data']['id']
    owner = 'sidebar-owner-' + suffix
    legacy = 'sidebar-legacy-' + suffix
    empty = 'sidebar-empty-' + suffix
    seed_conversation(owner, title='Owner conversation', settings={'folderId': folder_id, 'projectPath': project},
                      messages=[{'role': 'user', 'content': 'Owner conversation transcript'}])
    # New workers after initial render must create their Coordinator's group on
    # the authoritative catalog/push update, not depend on a preload refresh.
    live = 'sidebar-live-' + suffix
    # No _coordinatorSource: durable assignment history supplies this existing worker.
    seed_conversation(legacy, title='Legacy delegated worker', settings={'folderId': folder_id, 'projectPath': project},
                      messages=[{'role': 'user', 'content': 'Legacy delegated worker transcript'}])
    seed_conversation(empty, title='Empty delegated shell', settings={'folderId': folder_id, 'projectPath': project,
                      '_coordinatorSource': 'placeholder'})
    binding = page.request.get(origin + '/api/v4/project/coordination', params={'path': project}).json()['data']
    created = page.request.post(origin + '/api/v4/project/coordination/create',
                                headers={'Idempotency-Key': 'sidebar-group-' + suffix},
                                data={'path': project, 'folderId': folder_id, 'expectedRevision': binding['revision']})
    assert created.ok, created.text()
    leader = created.json()['data']['conversationId']
    from lib.conversations.settings_store import update_conversation_settings
    assert update_conversation_settings(empty, lambda settings: settings.update({'_coordinatorSource': leader}), user_id=1) is not None
    pair = turns.create_turn_pair(leader, user_id=1, command_id='sidebar-source-' + suffix,
                                  input_projection={'content': 'Review the sidebar'},
                                  config={'projectPath': project})
    turns.bind_task(pair['attempt']['attemptId'], 'sidebar-source-task-' + suffix, user_id=1)
    turns.mark_task_started(pair['attempt']['attemptId'], 'sidebar-source-task-' + suffix, user_id=1)
    source = turns.get_turn(leader, pair['submittedTurn']['turnId'], user_id=1)
    repository = ProjectDispatchRepository(1, project)
    repository.authorize({'projectPath': project, 'coordinatorId': leader, 'recipientId': legacy,
        'authorizationSource': {'conversationId': leader, 'turnId': source['turnId'],
                                'projectionRevision': source['projectionRevision']},
        'brief': 'Legacy sidebar work', 'model': 'sidebar-model', 'budget': {}},
        PrincipalContext.user(subject_id='user:1', owner_user_id=1, scopes=['chat']), 'sidebar-legacy-' + suffix)
    page.evaluate('localStorage.setItem("tofu_ui_lang", "en")')
    page.reload()
    _dismiss_onboarding_modals(page)
    page.set_viewport_size({'width': 1440, 'height': 960})
    page.locator(f'.folder-tab[data-folder-id="{folder_id}"]').click()
    seed_conversation(live, title='Live delegated worker', settings={'folderId': folder_id, 'projectPath': project,
                      '_coordinatorSource': leader}, messages=[{'role': 'user', 'content': 'Live delegated worker'}])
    # The group sits directly beneath its Coordinator's pinned entry.
    group = page.locator(f'#projectCoordinatorEntry + .coordinator-conversations-host '
                         f'.coordinator-conversations[data-coordinator-id="{leader}"]')
    toggle = group.locator('.coordinator-conversations-toggle')
    expect(toggle).to_have_attribute('aria-expanded', 'false')
    expect(toggle).to_contain_text('Conversations')
    expect(toggle.locator('.coordinator-conversations-count')).to_have_text('3')
    assert page.locator(f'#convList .conv-item[data-conv-id="{legacy}"], #convList .conv-item[data-conv-id="{empty}"]').count() == 0
    expect(page.locator('.conv-date-group[data-group="today"] .conv-date-count')).to_have_text('1')
    toggle.focus()
    expect(toggle).to_be_focused()
    page.keyboard.press('Enter')
    expect(toggle).to_have_attribute('aria-expanded', 'true')
    body = group.locator('.coordinator-conversations-body')
    expect(body.locator('.conv-item')).to_have_count(3)
    expect(body).to_contain_text('Legacy delegated worker')
    expect(body).to_contain_text('Empty delegated shell')
    expect(body).to_contain_text('Done')
    assert page.evaluate('(id) => localStorage.getItem(`tofu.coordinator.conversations.expanded.${id}`)', arg=leader) == 'true'
    output = Path(__file__).resolve().parents[2] / 'output/playwright/coordinator-sidebar'
    output.mkdir(parents=True, exist_ok=True)
    for theme, width in [('light', 1440), ('dark', 1440), ('tofu', 1440), ('light', 320), ('dark', 320)]:
        page.evaluate('(theme) => document.documentElement.dataset.theme = theme', theme)
        page.set_viewport_size({'width': width, 'height': 960})
        if width < 600:
            page.evaluate('''() => {
              const sidebar = document.getElementById("sidebar");
              if (sidebar.classList.contains("collapsed")) window.toggleSidebar();
            }''')
            page.wait_for_function('''() => {
              const bounds = document.querySelector("#sidebar").getBoundingClientRect();
              return bounds.left >= -1 && bounds.right > 0 &&
                !document.querySelector("#sidebar").classList.contains("collapsed");
            }''')
        page.locator('#sidebar').evaluate('(el) => el.getAnimations().map(animation => animation.finish())')
        for expanded in [False, True]:
            if width < 600:
                page.evaluate('''() => {
                  if (document.getElementById("sidebar").classList.contains("collapsed")) window.toggleSidebar();
                }''')
                page.wait_for_function('''() => {
                  const bounds = document.querySelector("#sidebar").getBoundingClientRect();
                  return bounds.left >= -1 && bounds.right > 0 &&
                    !document.querySelector("#sidebar").classList.contains("collapsed");
                }''')
            if expanded != (toggle.get_attribute('aria-expanded') == 'true'):
                try:
                    toggle.click(timeout=5000)
                except Exception:
                    diagnostic = page.evaluate('''() => ({
                      width: innerWidth,
                      sidebar: (() => { const r = document.querySelector("#sidebar").getBoundingClientRect(); return {left:r.left,right:r.right,width:r.width,scrollWidth:document.querySelector("#sidebar").scrollWidth,clientWidth:document.querySelector("#sidebar").clientWidth,classes:document.querySelector("#sidebar").className}; })(),
                      button: (() => { const r = document.querySelector(".coordinator-conversations-toggle").getBoundingClientRect(); return {left:r.left,right:r.right,width:r.width}; })(),
                      column: (() => { const r = document.querySelector(".coordination-conversation-column").getBoundingClientRect(); return {left:r.left,right:r.right,width:r.width,scrollWidth:document.querySelector(".coordination-conversation-column").scrollWidth,clientWidth:document.querySelector(".coordination-conversation-column").clientWidth}; })()
                    })''')
                    page.screenshot(path=str(output / 'mobile-click-failure.png'), animations='disabled')
                    raise AssertionError(diagnostic)
            expect(toggle).to_have_attribute('aria-expanded', str(expanded).lower())
            state = 'expanded' if expanded else 'collapsed'
            toggle.evaluate('(el) => el.getAnimations().map(animation => animation.finish())')
            page.locator('#sidebar').evaluate('(el) => el.getAnimations().map(animation => animation.finish())')
            page.screenshot(path=str(output / f'{theme}-{width}-{state}.png'), animations='disabled')
        assert page.evaluate('() => document.documentElement.scrollWidth <= document.documentElement.clientWidth + 1')
    page.set_viewport_size({'width': 1440, 'height': 960})
    page.reload()
    _dismiss_onboarding_modals(page)
    # The reload selects no folder; the group returns with its Coordinator's entry, still expanded.
    page.locator(f'.folder-tab[data-folder-id="{folder_id}"]').click()
    expect(group).to_have_count(1)
    expect(toggle).to_have_attribute('aria-expanded', 'true')
    expect(page.locator(f'.coordinator-conversations[data-coordinator-id="{leader}"]')).to_have_count(1)
    expect(page.locator('.coordinator-history-entry')).to_have_count(0)
    body.locator(f'.conv-item[data-conv-id="{empty}"]').click()
    page.wait_for_function('(id) => window.activeConvId === id', arg=empty)
    page.locator('#sidebarSearchToggle').click()
    page.locator('#sidebarSearchInput').fill(legacy)
    found = page.locator(f'#convList .conv-item[data-conv-id="{legacy}"]')
    found.wait_for(state='visible')
    expect(found.locator('.conv-coordinator-badge')).to_have_text('Project coordinator')
    found.locator('.conv-title').click(position={'x': 4, 'y': 6})
    page.wait_for_function('(id) => window.activeConvId === id', arg=legacy)
    page.evaluate('(id) => loadConversation(id)', empty)
    page.wait_for_function('(id) => window.activeConvId === id', arg=empty)
    direct = page.request.get(origin + '/api/v4/conversations', params={'meta': 1})
    assert direct.ok, direct.text()
    payload = direct.json()
    rows = payload.get('items', payload.get('data', {}).get('items'))
    assert rows is not None, payload
    items = {row['id']: row for row in rows}
    # Stored creation provenance is catalog metadata. A legacy worker without
    # that marker is resolved by the paged project roster below, never by an
    # unbounded dispatch-history scan during every catalog refresh.
    assert '_coordinatorSource' not in items[legacy]['settings']
    assert items[empty]['settings']['_coordinatorSource'] == leader
    roster = page.request.get(origin + '/api/v4/project/coordination/conversations', params={'path': project})
    assert roster.ok
    assert {row['conversationId'] for row in roster.json()['data']['conversations']} >= {legacy, empty}
    # The group has its own scrolling body; history never buries the owner's rows.
    for index in range(20):
        seed_conversation(f'sidebar-history-{index}-{suffix}', title=f'Older delegated work {index}',
                          settings={'folderId': folder_id, 'projectPath': project, '_coordinatorSource': leader},
                          messages=[{'role': 'user', 'content': f'Older delegated work {index}'}])
    page.evaluate('(id) => loadConversation(id)', leader)
    expect(group.locator('.coordinator-conversations-count')).to_have_text('23')
    expect(body.locator('.conv-item')).to_have_count(23)
    assert body.evaluate('(el) => el.scrollHeight > el.clientHeight')
    assert page.locator('#sidebar').evaluate('(el) => el.scrollWidth <= el.clientWidth + 1')


def test_sidebar_paints_each_coordinator_once_across_folder_switches(page, monkeypatch, tmp_path, assert_no_js_errors):
    """A loading binding never repaints the current Coordinator as a second row,
    a replaced Coordinator keeps exactly one row with its own group, leaving a
    folder removes everything painted for it, and the fold chevron points right
    while collapsed and down while open."""
    from lib.conversations.project_identity import normalize_project_path
    from tests._seed import seed_conversation
    from tests.conftest import _dismiss_onboarding_modals
    import routes.api_v1.folders as folders

    monkeypatch.setattr(folders, '_FOLDERS_PATH', str(tmp_path / 'folders.json'))
    origin = page.url.split('/?', 1)[0].rstrip('/')
    suffix = uuid.uuid4().hex
    projects, folder_ids = [], []
    for name in ['Alpha', 'Beta', 'Gamma']:
        (tmp_path / name).mkdir()
        projects.append(normalize_project_path(str(tmp_path / name)))
        response = page.request.post(origin + '/api/v4/folders', data={'name': name, 'projectPaths': [projects[-1]]})
        assert response.ok, response.text()
        folder_ids.append(response.json()['data']['id'])

    def create_coordinator(index, key):
        binding = page.request.get(origin + '/api/v4/project/coordination', params={'path': projects[index]})
        assert binding.ok, binding.text()
        created = page.request.post(origin + '/api/v4/project/coordination/create',
                                    headers={'Idempotency-Key': key + suffix},
                                    data={'path': projects[index], 'folderId': folder_ids[index],
                                          'expectedRevision': binding.json()['data']['revision']})
        assert created.ok, created.text()
        return created.json()['data']['conversationId']

    # A current-revision create replaces Alpha's first Coordinator; Beta has its own.
    replaced = create_coordinator(0, 'alpha-first-')
    current = create_coordinator(0, 'alpha-second-')
    beta = create_coordinator(1, 'beta-')
    workers = []
    for source, index in [(replaced, 0), (current, 0), (beta, 1)]:
        workers.append('worker-of-' + source)
        seed_conversation(workers[-1], title='Delegated by ' + source,
                          settings={'folderId': folder_ids[index], 'projectPath': projects[index],
                                    '_coordinatorSource': source},
                          messages=[{'role': 'user', 'content': 'Delegated work'}])
    page.evaluate('localStorage.setItem("tofu_ui_lang", "en")')
    page.reload()
    _dismiss_onboarding_modals(page)
    page.set_viewport_size({'width': 1440, 'height': 960})
    page.wait_for_function('(ids) => ids.every((id) => window.conversations.some((row) => row.id === id))',
                           arg=[replaced, current, beta, *workers])
    history = page.locator('.coordinator-history-entry .conv-item')
    groups = page.locator('.coordination-conversation-column .coordinator-conversations:not([hidden])')
    # Uncategorized holds none of these Coordinators, so it paints none of them.
    page.evaluate('() => renderConversationList()')
    painted = history.evaluate_all('(rows) => rows.map((row) => row.dataset.convId)')
    assert not set(painted) & {replaced, current, beta}

    leader_settled = '''(id) => {
        const row = document.querySelector('#projectCoordinatorEntry .coordination-leader');
        return row && !row.hasAttribute('aria-busy') && row.dataset.coordinatorId === id;
    }'''

    def visit(folder_id, leader, replaced_rows, switches_project=True):
        # Until a switched project's binding answers, no conversation can be called a
        # replaced Coordinator; a folder of the already-resolved project paints in the same tick.
        painted = page.evaluate('''(id) => {
            document.querySelector(`.folder-tab[data-folder-id="${id}"]`).click();
            return [...document.querySelectorAll('.coordinator-history-entry .conv-item')].map((row) => row.dataset.convId);
        }''', folder_id)
        assert painted == ([] if switches_project else replaced_rows)
        page.wait_for_function(leader_settled, arg=leader)
        expect(page.locator('#projectCoordinatorEntry + .coordinator-conversations-host .coordinator-conversations-toggle')).to_be_visible()
        expect(history).to_have_count(len(replaced_rows))
        assert history.evaluate_all('(rows) => rows.map((row) => row.dataset.convId)') == replaced_rows
        expect(groups).to_have_count(1 + len(replaced_rows))
        listed = set(page.locator('#convList .conv-item').evaluate_all('(rows) => rows.map((row) => row.dataset.convId)'))
        assert not listed & {leader, *replaced_rows, *workers}
        # Every conversation here sits in a Coordinator group, so the folder is not called empty.
        expect(page.locator('#convList .folder-view-empty')).to_have_count(0)

    # Boot restores an arbitrary conversation; pin the start to an Alpha worker opened
    # outside its folder, which resolves Alpha's binding before Alpha's folder is chosen.
    page.evaluate('(id) => loadConversation(id)', workers[1])
    page.wait_for_function(leader_settled, arg=current)
    visit(folder_ids[0], current, [replaced], switches_project=False)
    visit(folder_ids[1], beta, [])
    visit(folder_ids[0], current, [replaced])

    toggle = page.locator('#projectCoordinatorEntry + .coordinator-conversations-host .coordinator-conversations-toggle')
    pointing = '''(toggle) => {
        toggle.getAnimations({ subtree: true }).forEach((animation) => animation.finish());
        const chevron = toggle.querySelector('.coordinator-conversations-chevron');
        const values = chevron.querySelector('polyline').getAttribute('points').trim().split(/[\\s,]+/).map(Number);
        const [a, tip, b] = [0, 2, 4].map((index) => ({ x: values[index], y: values[index + 1] }));
        const transform = getComputedStyle(chevron).transform;
        const matrix = new DOMMatrix(transform === 'none' ? undefined : transform);
        const dx = tip.x - (a.x + b.x) / 2, dy = tip.y - (a.y + b.y) / 2;
        const x = matrix.a * dx + matrix.c * dy, y = matrix.b * dx + matrix.d * dy;
        return Math.abs(x) > Math.abs(y) ? (x > 0 ? 'right' : 'left') : (y > 0 ? 'down' : 'up');
    }'''
    expect(toggle).to_have_attribute('aria-expanded', 'false')
    assert toggle.evaluate(pointing) == 'right'
    toggle.click()
    expect(toggle).to_have_attribute('aria-expanded', 'true')
    assert toggle.evaluate(pointing) == 'down'
    output = Path(__file__).resolve().parents[2] / 'output/playwright/coordinator-sidebar'
    output.mkdir(parents=True, exist_ok=True)
    page.locator('#sidebar').screenshot(path=str(output / 'replaced-coordinator-expanded.png'), animations='disabled')
    toggle.click()
    expect(toggle).to_have_attribute('aria-expanded', 'false')
    assert toggle.evaluate(pointing) == 'right'
    page.locator('#sidebar').screenshot(path=str(output / 'replaced-coordinator-collapsed.png'), animations='disabled')
    visit(folder_ids[1], beta, [])
    page.locator(f'.folder-tab[data-folder-id="{folder_ids[2]}"]').click()
    expect(page.locator('#convList .folder-view-empty')).to_be_visible()


def test_manage_work_reads_server_history_rows(page, monkeypatch, tmp_path, assert_no_js_errors):
    """The list projection omits request fields; rework, pins and scope targets still work.

    Grants and reviews go through the dispatch repository and executions are
    settled Turns, so the browser reads the server's own history rows.
    """
    from lib import turn_lifecycle as turns
    from lib.conversations.project_identity import normalize_project_path
    from lib.identity import PrincipalContext
    from lib.project_dispatch.repository import ProjectDispatchRepository, _validate_schema
    from tests._seed import seed_conversation
    from tests.conftest import _dismiss_onboarding_modals
    import routes.api_v1.folders as folders

    monkeypatch.setattr(folders, '_FOLDERS_PATH', str(tmp_path / 'folders.json'))
    origin = page.url.split('/?', 1)[0].rstrip('/')
    suffix = uuid.uuid4().hex
    (tmp_path / 'Gamma').mkdir()
    project = normalize_project_path(str(tmp_path / 'Gamma'))
    folder = page.request.post(origin + '/api/v4/folders', data={'name': 'Gamma', 'projectPaths': [project]})
    assert folder.ok, folder.text()
    folder_id = folder.json()['data']['id']
    build, review = ['history-rows-' + kind + '-' + suffix for kind in ['build', 'review']]
    for cid, title in [(build, 'Parser worker'), (review, 'Review worker')]:
        seed_conversation(cid, title=title, settings={'folderId': folder_id, 'projectPath': project},
                          messages=[{'role': 'user', 'content': title + ' transcript'}])
    unbound = page.request.get(origin + '/api/v4/project/coordination', params={'path': project}).json()['data']
    created = page.request.post(origin + '/api/v4/project/coordination/create',
                                headers={'Idempotency-Key': 'history-rows-' + suffix},
                                data={'path': project, 'folderId': folder_id, 'expectedRevision': unbound['revision']})
    assert created.ok, created.text()
    leader = created.json()['data']['conversationId']

    def started_pair(conversation_id, label, content):
        pair = turns.create_turn_pair(conversation_id, user_id=1, command_id=label + suffix,
                                      input_projection={'content': content}, config={'projectPath': project})
        turns.bind_task(pair['attempt']['attemptId'], 'task-' + label + suffix, user_id=1)
        turns.mark_task_started(pair['attempt']['attemptId'], 'task-' + label + suffix, user_id=1)
        return pair

    source_pair = started_pair(leader, 'history-source-', 'Fix the parser and review its design')
    source = turns.get_turn(leader, source_pair['submittedTurn']['turnId'], user_id=1)
    repository = ProjectDispatchRepository(1, project)
    principal = PrincipalContext.user(subject_id='user:1', owner_user_id=1, scopes=['chat'])

    def grant(label, recipient, brief, **extra):
        return repository.authorize({
            'projectPath': project, 'coordinatorId': leader, 'recipientId': recipient,
            'authorizationSource': {'conversationId': leader, 'turnId': source['turnId'],
                                    'projectionRevision': source['projectionRevision']},
            'brief': brief, 'model': 'history-row-model', 'budget': {'maxTokens': 1000, 'maxModelRequests': 2},
            **extra}, principal, label + suffix)['id']

    def settle(assignment_id, recipient, label, content):
        pair = started_pair(recipient, label, 'Work on ' + assignment_id)
        assert turns.record_task_event({
            '_attemptId': pair['attempt']['attemptId'], '_userId': 1, 'id': 'task-' + label + suffix,
            'status': 'done', 'finishReason': 'stop', 'content': content, 'thinking': '', 'toolRounds': [],
            'config': {}}, {'type': 'done', 'finishReason': 'stop'})
        repository.bind_execution(assignment_id, pair['turn']['turnId'], pair['attempt']['attemptId'])

    def history(**query):
        response = page.request.get(origin + '/api/v4/project/coordination/assignments',
                                    params={'path': project, **query})
        assert response.ok, response.text()
        return response.json()['data']

    parser = grant('history-parser-', build, 'Fix the parser', profile={
        'mode': 'files', 'tools': ['read_files', 'edit_file'], 'readPaths': ['src'], 'writePaths': ['src'],
        'maxToolCalls': 8})
    settle(parser, build, 'history-parser-run-', 'Patched src/parser.py')
    design = grant('history-design-', review, 'Review the parser design')
    settle(design, review, 'history-design-run-', 'The design holds')
    notes = grant('history-notes-', review, 'Check the release notes')
    result = next(row for row in history()['assignments'] if row['assignmentId'] == parser)['execution']['resultRevision']
    repository.record_review(parser, 'rework', 'Cover empty input', result, 1)
    # The browser receives exactly the contract's list rows; an exact lookup keeps the whole grant.
    listed = history()
    _validate_schema(listed, 'historyPage')
    rows = {row['assignmentId']: row for row in listed['assignments']}
    assert {key: (row['execution'] or {}).get('status') for key, row in rows.items()} == {
        parser: 'completed', design: 'completed', notes: None}
    omitted = {'authorizationSource', 'model', 'budget'}
    assert not omitted & rows[parser]['request'].keys()
    assert omitted <= history(assignmentId=parser)['assignments'][0]['request'].keys()

    page.evaluate('localStorage.setItem("tofu_ui_lang", "en")')
    page.reload()
    _dismiss_onboarding_modals(page)
    page.set_viewport_size({'width': 1440, 'height': 960})
    page.locator(f'.folder-tab[data-folder-id="{folder_id}"]').click()
    page.locator(f'#projectCoordinatorEntry .coordination-leader[data-coordinator-id="{leader}"]').click()
    page.wait_for_function('(id) => window.activeConvId === id', arg=leader)
    # Worker records live behind the explicit management view, never in the chat.
    board = page.locator('#presenceStrip .coordinator-progress')
    board.wait_for(state='visible')
    assert page.locator('#presenceStrip .coordination-task-graph').count() == 0
    board.get_by_role('button', name='Manage work', exact=True).click()
    workspace = page.locator('dialog[open]')
    workspace.get_by_role('tab', name='Manage work', exact=True).click()
    dispatch = workspace.locator('[data-testid="project-dispatch"]')
    receipts = dispatch.locator('article')
    expect(receipts).to_have_count(3)
    receipts.filter(has_text='Fix the parser').get_by_role(
        'button', name='Prepare fresh rework authorization · draft only', exact=True).click()
    form = dispatch.locator('form')
    assert [form.locator(f'[name="{name}"]').input_value()
            for name in ['recipientId', 'brief', 'mode', 'readPaths', 'writePaths']] == [
        build, 'Fix the parser', 'files', 'src', 'src']
    # The row's profile.tools restores the file-tool choices of the reworked grant.
    assert [form.locator(f'[name="{name}"]').is_checked() for name in ['read_files', 'write_file', 'edit_file']] == [
        True, False, True]
    expect(form).to_contain_text(f'Rework of {parser} · grant 1 · result {result}.')
    dependencies = dispatch.locator('[data-testid="dispatch-dependencies"]')
    pin = dependencies.locator(f'input[name="dependency"][data-assignment-id="{design}"]')
    pin.click()
    expect(pin).to_be_checked()
    expect(dependencies).to_contain_text('1 / 8 selected')
    # A completed file result without a checkpoint is listed but cannot be pinned.
    expect(dependencies.locator('input[name="dependency-unavailable"]')).to_be_disabled()
    expect(dependencies).to_contain_text(f'{parser} · File reference unavailable')
    scope_changes = workspace.locator('[data-testid="project-scope-changes"]')
    targets = scope_changes.locator('input[name="scopeTarget"]')
    expect(targets).to_have_count(3)
    assert sorted(targets.evaluate_all('(inputs) => inputs.map((input) => input.dataset.assignmentId)')) == sorted(
        [parser, design, notes])
    expect(scope_changes).to_contain_text(f'{notes} · r1 · Check the release notes')
    workspace.get_by_role('button', name='Back to conversation', exact=True).click()
    expect(workspace).to_have_count(0)
