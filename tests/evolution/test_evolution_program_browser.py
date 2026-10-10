"""Actual shell/HTTP/program/run/episode integration; only costly model tasks are synthetic."""
import json
import re
import uuid
from pathlib import Path

import pytest
from tests.evolution.test_evolution_runs import runtime as _fixture_runtime

task_runtime = _fixture_runtime

pytestmark = pytest.mark.visual


@pytest.mark.parametrize('width', [390, 1440])
def test_execution_options_freeze_roles_and_restore_drafts_in_real_storage(browser, live_server, stored_program_runtime, monkeypatch, width):
    """Real form/HTTP/native records; catalog and model task execution are controlled."""
    from types import SimpleNamespace
    from playwright.sync_api import expect
    from tests.conftest import _dismiss_onboarding_modals
    from lib.evolution import programs
    runs, _, tasks = stored_program_runtime
    executor_ref = {'creator_id': 'controlled', 'model_id': 'executor'}
    reviewer_ref = {'provider_id': 'controlled', 'offering_id': 'reviewer'}
    catalog = [{'model': executor_ref, 'label': 'Controlled executor'},
               {'model': reviewer_ref, 'label': 'Controlled independent reviewer'}]
    monkeypatch.setattr('lib.evolution.workspace.model_options', lambda **kw: catalog)
    monkeypatch.setattr('lib.evolution.role_models.model_options', lambda **kw: catalog)
    monkeypatch.setattr('lib.model_routing.mint_routed_slot_group',
        lambda *args, **kw: SimpleNamespace(pin_id='controlled-role-pin',
            candidates=[SimpleNamespace(offering={'pending_model_id': 'reviewer'})]))
    monkeypatch.setattr('lib.model_routing.dispose_routed_slot_group', lambda *_: None)
    context = browser.new_context(viewport={'width': width, 'height': 960})
    context.add_init_script("localStorage.setItem('tofu_onboarding_v1_done','1')")
    page = context.new_page(); errors = []; creates = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.on('request', lambda request: creates.append(request) if request.method == 'POST' and request.url.endswith('/programs') else None)
    try:
        page.goto(live_server + '/#/evolution/programs/new'); _dismiss_onboarding_modals(page)
        expect(page.locator('.exp-create-form')).to_be_visible(timeout=20000)
        details = page.locator('.exp-create-form details.exp-execution-options')
        details.locator('summary').click()
        page.locator('[name=program-token-limit]').fill('500')
        page.locator('[name=program-tool-limit]').fill('20')
        page.locator('[name=program-hours]').fill('0.1')
        page.locator('[name=program-attempts]').fill('1')
        page.locator('[name=program-executor-model]').select_option(label='Controlled executor')
        page.locator('[name=program-reviewer-model]').select_option(label='Controlled independent reviewer')
        selected_reviewer_serialized = page.locator('[name=program-reviewer-model]').input_value()
        # Settings alone survive reload; restoration never starts a program.
        page.reload(); expect(page.locator('.exp-create-form')).to_be_visible(timeout=20000)
        details.locator('summary').click()
        expect(page.locator('[name=program-token-limit]')).to_have_value('500')
        expect(page.locator('[name=program-reviewer-model]')).to_have_value(selected_reviewer_serialized)
        assert not creates and not tasks
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1')
        assert page.locator('.exp-main').evaluate('el => el.scrollWidth <= el.clientWidth + 1')
        creator_shot = Path('output/playwright') / f'program-execution-options-creator-{width}.png'
        creator_shot.parent.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(creator_shot), full_page=True)
        page.locator('[name=objective]').fill(f'Controlled execution policy persistence {width}; user value remains unknown.')
        page.locator('[name=acceptance]').fill('Preserve the original options across reload.')
        with page.expect_response(lambda response: response.url.endswith('/commands')) as started:
            page.locator('.exp-create-form button[type=submit]').click()
        assert started.value.status == 200
        expect(page.locator('.exp-detail')).to_be_visible(timeout=20000)
        pid = page.url.split('/programs/')[1]; executor = tasks[0]; owner = executor['_userId']
        header = programs.get_program(pid, user_id=owner)
        expected = {'budget': {'max_tokens': 500, 'max_tool_calls': 20, 'max_wall_clock_seconds': 360},
                    'max_attempts': 1, 'role_models': {'executor': executor_ref, 'reviewer': reviewer_ref}}
        assert {key: header['execution_options'][key] for key in expected} == expected
        assert executor['config']['modelRef'] == executor_ref
        assert creates[0].post_data_json['budget'] == expected['budget']
        frozen = page.locator('.exp-original details.exp-execution-options')
        frozen.locator('summary').click()
        expect(frozen).to_contain_text('Controlled independent reviewer')
        expect(frozen).to_contain_text(re.compile('pending|待当前实验结算', re.I))
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1')
        assert page.locator('.exp-main').evaluate('el => el.scrollWidth <= el.clientWidth + 1')
        shot = Path('output/playwright') / f'program-execution-options-{width}.png'
        shot.parent.mkdir(parents=True, exist_ok=True); page.screenshot(path=str(shot), full_page=True)
        # Pin the independent reviewer at the actual shared task boundary.
        executor.update(status='done', finishReason='stop', content='Controlled observation; no value verdict.',
            finished_at=executor['created_at'] + 1, usage={'input_tokens': 20, 'output_tokens': 10})
        run = runs.advance_run(header['current']['run_id'], user_id=owner, terminal_task=executor)
        assert run['status'] == 'reviewing' and len(tasks) == 2
        assert tasks[-1]['config']['modelRef'] == reviewer_ref
        page.reload(); expect(page.locator('.exp-detail')).to_be_visible(timeout=20000)
        frozen.locator('summary').click(); expect(frozen).to_contain_text('Controlled independent reviewer')
        assert programs.get_program(pid, user_id=owner)['execution_options'] == header['execution_options']
        with page.expect_response(lambda response: response.url.endswith('/commands')) as cancelled:
            page.locator('[data-program-action=cancel]').click()
        assert cancelled.value.status == 200
        final = programs.get_program(pid, user_id=owner)
        assert final['status'] == 'cancelled' and final['execution_options'] == header['execution_options']
        assert len(creates) == 1 and len(tasks) == 2 and not errors
        (shot.with_suffix('.json')).write_text(json.dumps({'width': width, 'program_id': pid, 'owner': owner,
            'header': final, 'scope': 'Real form/HTTP/native records; two controlled task births, zero models'}, indent=2))
    finally:
        context.close()


@pytest.fixture
def stored_program_runtime(monkeypatch, request):
    """Keep real semantic records/episodes; only model execution is synthetic."""
    from lib.storage import storage_status
    if not storage_status().get('ready'):
        request.getfixturevalue('chat_sidecar')
    from tests.conftest import _assert_isolated_storage
    from lib.evolution import records, runs
    from lib.storage import get_storage_client
    from lib.tasks_pkg import spawn
    _assert_isolated_storage('stored exploration browser journey')
    assert records._storage(write=True) is get_storage_client(write=True)
    monkeypatch.setattr(runs, 'resolve_run_model', lambda requested, **kw: requested)
    tasks = []
    monkeypatch.setattr(spawn, 'spawn_task', lambda task: tasks.append(task))
    monkeypatch.setattr(runs, 'default_task_loader', lambda tid, user_id:
        next((task for task in tasks if task['id'] == tid and task['_userId'] == user_id), None))
    yield runs, None, tasks
    from lib.tasks_pkg.manager._registry import discard_task
    from lib.agent_core.execution_session import ExecutionPhase
    for task in tasks:
        task['_executionSession'].settle(ExecutionPhase.CANCELLED, cause='test_cleanup')
        discard_task(task['id'], '')


@pytest.mark.parametrize('width', [390, 1440])
def test_declared_artifact_survives_live_file_removal_in_real_storage(browser, live_server, stored_program_runtime, tmp_path, width):
    """Real native/HTTP/UI sources; controlled executor, no network model claim."""
    import hashlib
    from playwright.sync_api import expect
    from tests.conftest import _dismiss_onboarding_modals
    from lib.evolution import artifact_evidence, stage_outcomes

    runs, _, tasks = stored_program_runtime
    context = browser.new_context(viewport={'width': width, 'height': 960})
    context.add_init_script("localStorage.setItem('tofu_onboarding_v1_done','1')")
    page = context.new_page(); errors = []; reads = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.on('request', lambda request: reads.append(request) if '/outcomes/' in request.url else None)
    try:
        response = context.request.post(live_server+'/api/v4/evolution/runs',
            data={'objective': 'Controlled artifact delivery; value remains unverified.',
                  'project_path': str(tmp_path), 'max_attempts': 1})
        assert response.status == 201
        run = response.json()['data']['run']; executor = tasks[-1]; owner = executor['_userId']
        original = ('Original generated observation <script>throw 1</script> 🧪\n' * 650) + 'DECLARED_ARTIFACT_TAIL'
        path = tmp_path/'observations.txt'; path.write_text(original)
        snapshot = artifact_evidence.declare(executor, {'path': 'observations.txt'})
        executor.update(status='done', finishReason='stop', content='Declared complete observations.txt.',
            messages=[{'role':'tool','tool_call_id':'head','content':'Original generated observation head only'}],
            usage={'input_tokens':20,'output_tokens':10}, finished_at=executor['created_at']+1)
        run = runs.advance_run(run['run_id'], user_id=owner, terminal_task=executor)
        assert run['status'] == 'reviewing' and run['artifact_evidence_source']['complete']
        path.write_text('Changed after declaration'); path.unlink()
        offset = 0; chunks = []
        while True:
            part = stage_outcomes.page(run['run_id'],executor['id'],user_id=owner,
                                      source_kind='artifact_evidence',offset=offset)
            chunks.append(part['content'])
            if part['next_offset'] is None: break
            offset = part['next_offset']
        source = ''.join(chunks)
        assert json.loads(source)['content'] == original
        assert hashlib.sha256(original.encode()).hexdigest() == snapshot['content_sha256']
        assert hashlib.sha256(source.encode()).hexdigest() == run['artifact_evidence_source']['source_sha256']
        page.goto(live_server+'/#/evolution/runs/'+run['run_id'])
        _dismiss_onboarding_modals(page)
        expect(page.locator('.evo-mission-panel')).to_contain_text(run['objective'], timeout=20000)
        page.locator('.evo-ledger-head[data-studio-action=details]').click()
        page.locator(f'[data-outcome-task-id="{executor["id"]}"]').click()
        choice = page.locator('[data-source-action=artifact_evidence]')
        expect(choice).to_be_visible(); choice.click()
        visible = page.locator('.evo-outcome-reader pre')
        expect(visible).to_have_text(source[:6000])
        expect(choice).to_have_attribute('aria-pressed','true')
        expect(page.locator('.evo-outcome-reader')).to_contain_text(re.compile('saved when declared|声明时保存'))
        assert page.locator('.evo-outcome-reader script').count() == 0
        offset = 6000
        while offset < len(source):
            page.locator('[data-source-action=next]').click()
            expect(visible).to_have_text(source[offset:offset+6000]); offset += 6000
        expect(visible).to_contain_text('DECLARED_ARTIFACT_TAIL')
        expect(page.locator('[data-source-action=next]')).to_be_disabled()
        page.locator('[data-source-action=tool_evidence]').click()
        expect(visible).to_contain_text('head only')
        page.locator('[data-source-action=artifact_evidence]').click()
        expect(visible).to_have_text(source[:6000])
        assert any('source_kind=artifact_evidence' in request.url for request in reads)
        assert all(request.method == 'GET' for request in reads)
        assert len(tasks) == 2 and not errors
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 2')
        screenshot = Path('output/playwright')/f'declared-artifact-native-{width}.png'
        screenshot.parent.mkdir(parents=True,exist_ok=True); page.screenshot(path=str(screenshot))
        page.reload()
        expect(page.locator('.evo-mission-panel')).to_contain_text(run['objective'],timeout=20000)
        page.locator('.evo-ledger-head[data-studio-action=details]').click()
        page.locator(f'[data-outcome-task-id="{executor["id"]}"]').click()
        page.locator('[data-source-action=artifact_evidence]').click()
        expect(page.locator('.evo-outcome-reader pre')).to_have_text(source[:6000])
        assert len(tasks) == 2 and not path.exists()
    finally:
        context.close()


@pytest.mark.parametrize('width', [390, 1440])
def test_catalog_refresh_keeps_keyboard_focus_and_navigation(browser, live_server, monkeypatch, width):
    """Real owner storage/HTTP with a held real GET; no executor or model."""
    from playwright.sync_api import expect
    from tests.conftest import _dismiss_onboarding_modals
    from lib.storage import get_storage_client, storage_status
    from lib.evolution import programs, records

    # The module's real storage also retains other journeys. Give this
    # controlled HTTP actor a separate owner instead of deleting foreign rows.
    owner = 1000 + uuid.uuid4().int % 1_000_000_000
    monkeypatch.setattr('routes.api_v1.evolution.request_user_id', lambda: owner)

    context = browser.new_context(viewport={'width':width,'height':960})
    context.add_init_script("localStorage.setItem('tofu_onboarding_v1_done','1')")
    page = context.new_page(); errors = []; ids = []; held = []; hold = [False]; reads = []; keys = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.on('console', lambda message: keys.append(message.text) if message.text.startswith('PROGRAM_KEY ') else None)
    page.on('request', lambda request: reads.append(request) if '/evolution/' in request.url else None)
    token = uuid.uuid4().hex
    client = get_storage_client(write=True)
    def catalog_response(route):
        if hold[0] and route.request.method == 'GET':
            # Hold one background read only. Its replacement must be free to
            # finish even if the real fetch callback completes after abort.
            hold[0] = False
            held.append((route, route.fetch()))
        else:
            route.continue_()
    context.route('**/api/v4/evolution/programs?*', catalog_response)
    artifact = Path('output/playwright') / f'program-keyboard-{width}'
    artifact.parent.mkdir(parents=True, exist_ok=True)
    try:
        for index in range(27):
            response = context.request.post(live_server+'/api/v4/evolution/programs',
                headers={'Idempotency-Key':f'keyboard-{token}-{index}'},
                data={'objective':'Keyboard long exploration with an identical objective'})
            assert response.status == 201
            ids.append(response.json()['data']['program']['program_id'])
        ids.sort()
        # Equal goal labels remain separate ordinary admissions. Saved creation
        # times are controlled fixture metadata, not an execution claim.
        for index, program_id in enumerate(ids):
            row, version = programs._read(program_id, owner)
            row['created_at'] = 1700000000 + index * 60
            programs._save(row, version, owner)
        # Saved synthetic receipts exercise historical reading only. They do
        # not represent executed or independently reviewed model experiments.
        selected, version = programs._read(ids[0], owner)
        selected['settled_through'] = 41
        programs._save(selected, version, owner)
        for sequence in range(1, 42):
            receipt = {'schema_version':1, 'program_id':ids[0], 'sequence':sequence,
                'step':selected['queue'][0], 'run_id':f'fixture-{token}-{sequence}',
                'outcome':'completed', 'result':f'Synthetic original evidence {sequence}',
                'review':None, 'actuals':{'tokens':0,'tool_calls':0,'wall_clock_seconds':0},
                'error':'', 'created_at':sequence}
            programs._validate(receipt, 'receipt')
            records.put_record(programs.EXPERIMENTS, programs._experiment_key(ids[0],sequence,owner),
                receipt, kind='experiment', expected_version=0)
        page.goto(live_server+'/#/evolution/programs/'+ids[0])
        _dismiss_onboarding_modals(page)
        expect(page.locator('.exp-detail')).to_be_visible(timeout=20000)
        expect(page.locator('.exp-catalog [data-program-id]')).to_have_count(20)
        entries = page.locator('.exp-catalog [data-program-id]')
        expect(entries.first.locator('.exp-program-meta')).to_be_visible()
        expect(entries.first.locator('time')).to_have_attribute('datetime','2023-11-14T22:13:20.000Z')
        expect(entries.nth(1).locator('time')).to_have_attribute('datetime','2023-11-14T22:14:20.000Z')
        assert entries.first.locator('.exp-program-meta').inner_text() != entries.nth(1).locator('.exp-program-meta').inner_text()
        page.locator('.exp-expansion summary').click()
        draft = page.locator('[name=instruction]'); draft.fill('Unsubmitted future mechanism')
        draft_node = draft.element_handle()
        next_page = page.locator('.exp-catalog-pager button').nth(1)
        next_page.evaluate("""node => ['keydown','keyup','click'].forEach(kind => node.addEventListener(kind,
          event => console.log('PROGRAM_KEY '+JSON.stringify({type:kind,key:event.key,
            prevented:event.defaultPrevented,disabled:node.disabled,aria:node.getAttribute('aria-disabled')}))))""")
        next_page.focus(); expect(next_page).to_be_focused()
        hold[0] = True
        with page.expect_request(lambda request:'/evolution/programs?' in request.url):
            page.evaluate("document.dispatchEvent(new Event('visibilitychange'))")
        page.wait_for_function("document.querySelector('.exp-catalog-pager button:nth-child(2)').disabled || document.querySelector('.exp-catalog-pager').getAttribute('aria-busy') === 'true'")
        focus = next_page.evaluate('(node)=>({focused:document.activeElement===node,disabled:node.disabled,activeTag:document.activeElement.tagName})')
        artifact.with_suffix('.json').write_text(json.dumps({'width':width,'authority':storage_status(),
            'focus_during_real_get':focus,'scope':'Synthetic saved programs, actual owner storage/HTTP/browser; held real GET response, zero executor/model'},indent=2))
        page.screenshot(path=str(artifact.with_suffix('.png')), full_page=True)
        expect(next_page).to_be_focused()
        # Navigation is allowed to supersede a background read. The pending
        # page cannot be skipped by repeated native keyboard activation.
        next_page.press('Enter')
        hold[0] = False
        for route, response in held:
            route.fulfill(response=response)
        held.clear()
        expect(page.locator('.exp-catalog [data-program-id]').first).to_have_attribute('data-program-id',ids[20])
        expect(next_page).to_be_focused()
        expect(next_page).to_have_attribute('aria-disabled','true')
        next_page.press('Enter')
        expect(page.locator('.exp-catalog [data-program-id]').first).to_have_attribute('data-program-id',ids[20])
        page.keyboard.press('Shift+Tab')
        previous = page.locator('.exp-catalog-pager button').nth(0)
        expect(previous).to_be_focused(); previous.press('Enter')
        expect(page.locator('.exp-catalog [data-program-id]').first).to_have_attribute('data-program-id',ids[0])
        expect(previous).to_be_focused()
        expect(previous).to_have_attribute('aria-disabled','true')
        expect(page.locator('.exp-receipt').first).to_have_attribute('data-sequence','22')
        older = page.locator('.exp-pager button').nth(0)
        older.focus(); expect(older).to_be_focused()
        hold[0] = True
        with page.expect_request(lambda request:'/evolution/programs?' in request.url):
            page.evaluate("document.dispatchEvent(new Event('visibilitychange'))")
        expect(page.locator('.exp-catalog-pager')).to_have_attribute('aria-busy','true')
        expect(older).to_be_focused(); older.press('Enter')
        expect(page.locator('.exp-detail')).to_be_visible()
        expect(older).to_be_focused()
        hold[0] = False
        for route, response in held: route.fulfill(response=response)
        held.clear()
        expect(page.locator('.exp-receipt').first).to_have_attribute('data-sequence','2')
        expect(older).to_be_focused()
        # Reach the first receipt and keep focus on its unavailable older
        # control; its native key and pointer actions cannot dispatch a read.
        older.press('Enter')
        expect(page.locator('.exp-receipt').first).to_have_attribute('data-sequence','1')
        expect(older).to_be_focused(); expect(older).to_have_attribute('aria-disabled','true')
        older.press('Enter'); older.click(force=True)
        expect(page.locator('.exp-receipt').first).to_have_attribute('data-sequence','1')
        expect(draft).to_have_value('Unsubmitted future mechanism')
        assert draft_node.evaluate('node=>node===document.querySelector("[name=instruction]")')
        assert page.url.endswith(ids[0]) and not errors
        assert all(request.method == 'GET' for request in reads)
        evidence = json.loads(artifact.with_suffix('.json').read_text())
        evidence.update(catalog_keyboard_navigation=True,history_keyboard_navigation=True,
            stable_draft=True,read_only_requests=True,synthetic_receipts=41)
        artifact.with_suffix('.json').write_text(json.dumps(evidence,indent=2))
        page.screenshot(path=str(artifact.with_suffix('.png')),full_page=True)
    finally:
        hold[0] = False
        for route, response in held:
            try: route.fulfill(response=response)
            except Exception: pass
        if artifact.with_suffix('.json').exists():
            evidence = json.loads(artifact.with_suffix('.json').read_text())
            evidence.update(key_events=keys,read_requests=[{'method':request.method,'url':request.url}
                for request in reads if '/evolution/programs?' in request.url],page_errors=errors)
            artifact.with_suffix('.json').write_text(json.dumps(evidence,indent=2))
        context.close()
        for program_id in ids:
            client.command('record.delete', {'namespace':programs.PROGRAMS,
                'key':programs._key(program_id, owner)},
                command_id=f'browser-program-delete-{token}-{program_id}')
        if ids:
            for sequence in range(1, 42):
                client.command('record.delete', {'namespace':programs.EXPERIMENTS,
                    'key':programs._experiment_key(ids[0], sequence, owner)},
                    command_id=f'browser-receipt-delete-{token}-{sequence}')


@pytest.mark.parametrize('width', [390, 1440])
def test_catalog_beyond_first_window_keeps_selected_program_and_draft(browser, live_server, task_runtime, width):
    from playwright.sync_api import expect
    from tests.conftest import _dismiss_onboarding_modals
    _, store, tasks = task_runtime
    context = browser.new_context(viewport={'width':width,'height':960})
    context.add_init_script("localStorage.setItem('tofu_onboarding_v1_done','1')")
    page = context.new_page(); errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    try:
        page.goto(live_server+'/#/evolution');_dismiss_onboarding_modals(page)
        expect(page.locator('.exp-create-form')).to_be_visible(timeout=20000)
        ids = []
        for index in range(53):
            response = page.request.post(live_server+'/api/v4/evolution/programs',
                headers={'Idempotency-Key':f'browser-catalog-{width}-{index}'},
                data={'objective':f'真实问题与长期探索项目 {index}'})
            assert response.status == 201
            ids.append(response.json()['data']['program']['program_id'])
        ids.sort()
        page.goto(live_server+'/#/evolution/programs/'+ids[0])
        expect(page.locator('.exp-detail')).to_be_visible(timeout=20000)
        expect(page.locator('.exp-catalog [data-program-id]')).to_have_count(20)
        page.locator('.exp-objective summary').click()
        page.locator('.exp-expansion summary').click()
        draft = page.locator('[name=instruction]');draft.fill('保留尚未提交的新机制实验');draft.focus()
        element = draft.element_handle()
        writes = store.writes
        seen = []
        for index in range(3):
            seen.extend(page.locator('.exp-catalog [data-program-id]').evaluate_all('nodes=>nodes.map(node=>node.dataset.programId)'))
            assert element.evaluate('node=>node===document.querySelector("[name=instruction]")')
            expect(draft).to_have_value('保留尚未提交的新机制实验')
            assert page.locator('.exp-objective').evaluate('node=>node.open')
            if index < 2:
                with page.expect_response(lambda response:'/evolution/programs?' in response.url and 'after=' in response.url):
                    page.locator('.exp-catalog-pager button').nth(1).click()
                expect(page.locator('.exp-catalog [data-program-id]').first).to_have_attribute('data-program-id',ids[(index+1)*20])
        assert seen == ids
        expect(page.locator('.exp-catalog-pager button').nth(1)).to_be_disabled()
        page.locator('.exp-catalog-pager button').nth(0).click()
        expect(page.locator('.exp-catalog [data-program-id]').first).to_have_attribute('data-program-id',ids[20])
        page.locator('.exp-catalog-pager button').nth(2).click()
        expect(page.locator('.exp-catalog [data-program-id]').first).to_have_attribute('data-program-id',ids[0])
        assert page.url.endswith(ids[0]) and store.writes == writes and tasks == []
        assert not errors and page.evaluate('document.documentElement.scrollWidth<=innerWidth+2')
        artifact=Path('output/playwright')/f'program-catalog-{width}.png'
        artifact.parent.mkdir(parents=True,exist_ok=True);page.screenshot(path=str(artifact),full_page=True)
    finally:
        context.close()


@pytest.mark.parametrize('background', [False, True])
@pytest.mark.parametrize('width', [390, 1440])
def test_committed_terminal_original_survives_recovery_and_is_readable_beyond_inline_limit(browser, live_server, task_runtime, monkeypatch, width, background):
    """Real owner HTTP/storage/UI; terminal model output is explicitly synthetic."""
    from playwright.sync_api import expect
    from tests.conftest import _dismiss_onboarding_modals
    from lib.evolution.stage_outcomes import capture
    runs, _, tasks = task_runtime
    context = browser.new_context(viewport={'width': width, 'height': 960})
    context.add_init_script("localStorage.setItem('tofu_onboarding_v1_done','1')")
    page = context.new_page();errors = [];source_reads = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.on('request', lambda request: source_reads.append(request) if '/outcomes/' in request.url else None)
    handle = None
    try:
        page.goto(live_server + '/#/evolution/runs', wait_until='domcontentloaded')
        _dismiss_onboarding_modals(page)
        expect(page.locator('.evo-studio')).to_be_visible(timeout=20000)
        created = page.evaluate("""async () => {
            const response = await fetch('/api/v4/evolution/runs', {method: 'POST',
              headers: {'Content-Type': 'application/json'},
              body: JSON.stringify({objective: 'Inspect a previous experiment before proposing a mechanism.'})});
            return {status: response.status, body: await response.json()};
        }""")
        assert created['status'] == 201
        run = created['body']['data']['run'];executor = next(task for task in tasks if task['id'] == run['task_id'])
        owner = executor['_userId']
        content = ('Synthetic prior experiment: <img src=x> 🧪e\u0301\n' * 900) + 'ARCHIVED_TAIL_PROOF'
        assert len(content) > 24000
        retained_tool = 'Synthetic measured tools: <script>throw 1</script> 🧪e\u0301\n' * 180
        tool_jsonl = json.dumps({'message_index': 0, 'tool_call_id': 'saved-probe',
                                  'content': retained_tool}, ensure_ascii=False,
                                 sort_keys=True, separators=(',', ':')) + '\n'
        executor.update(status='done', finishReason='stop', content=content,
                        messages=[{'role': 'tool', 'tool_call_id': 'saved-probe', 'content': retained_tool}],
                        usage={'input_tokens': 20, 'output_tokens': 10},
                        finished_at=executor['created_at'] + 1, _executionActualsComplete=False)
        if background:
            import threading
            import uuid
            from lib.tasks_pkg import background_commands as commands
            handle='bg_'+uuid.uuid4().hex[:12]
            executor['messages']=[]
            executor['toolRounds']=[{'toolName':'run_command','toolCallId':'background-launch','status':'done',
                'results':[{'exitCode':'background','backgroundCommandId':handle}]}]
            assert commands.register_handed_off_command(command_id=handle,task=executor,command='experiment',done=threading.Event())
            completed='$ experiment\n'+retained_tool+'\n[exit code: 0]'
            commands.finish_handed_off_command(command_id=handle,result=completed,error=None)
            tool_jsonl=json.dumps({'tool_call_id':'background:'+handle,'name':'run_command',
                'source':'owned_background_command_completion','command_id':handle,
                'source_tool_call_id':'background-launch','content':completed,'error':None},
                ensure_ascii=False,sort_keys=True,separators=(',',':'))+'\n'
        capture(run, executor, user_id=owner, review_prompt_factory=lambda text: runs._review_prompt({**run, 'result': text}, executor))
        if handle:
            with commands._lock:commands._registry.pop(handle,None)
        loader = runs.default_task_loader
        monkeypatch.setattr(runs, 'default_task_loader', lambda tid, user_id:
                            None if tid == executor['id'] else loader(tid, user_id=user_id))
        recovered = runs.advance_run(run['run_id'], user_id=owner)
        assert recovered['status'] == 'reviewing' and recovered['receipts'][0]['restored_from_outcome'] is True
        assert recovered['result_source']['total_chars'] == len(content)
        page.goto(live_server + '/#/evolution/runs/' + run['run_id'])
        expect(page.locator('.evo-mission-panel')).to_contain_text(run['objective'], timeout=20000)
        page.locator('.evo-ledger-head[data-studio-action=details]').click()
        trigger = page.locator(f'[data-outcome-task-id="{executor["id"]}"]')
        expect(trigger).to_be_visible(timeout=20000)
        with page.expect_response(lambda response: '/outcomes/' in response.url):
            trigger.click()
        original = page.locator('.evo-outcome-reader pre')
        expect(original).to_have_text(content[:6000])
        expect(page.locator('.evo-outcome-reader')).to_contain_text('≥ 30')
        expect(page.locator('.evo-outcome-reader')).to_contain_text(re.compile('Recovered from a saved outcome|从已保存的成果恢复'))
        assert page.locator('.evo-outcome-reader img').count() == 0
        page.locator('[data-source-action=next]').click()
        expect(original).to_have_text(content[6000:12000])
        original.focus();element = original.element_handle()
        original.evaluate('node => window.getSelection().selectAllChildren(node)')
        updated, version = runs._read(run['run_id'], owner)
        updated['error'] += '; synthetic poll update'
        runs._save(updated, version, owner)
        with page.expect_response(lambda response: '/runs/' + run['run_id'] in response.url and '/outcomes/' not in response.url):
            page.evaluate("document.dispatchEvent(new Event('visibilitychange'))")
        expect(page.locator('.evo-mission-diagnosis pre')).to_contain_text('synthetic poll update')
        assert element.evaluate('node => node === document.querySelector(".evo-outcome-reader pre")')
        expect(original).to_be_focused()
        assert page.evaluate('window.getSelection().toString()') == content[6000:12000]
        assert len(source_reads) == 2
        expected_offset = 12000
        while expected_offset < len(content):
            page.locator('[data-source-action=next]').click()
            expect(original).to_have_text(content[expected_offset:expected_offset + 6000])
            expected_offset += 6000
        expect(original).to_contain_text('ARCHIVED_TAIL_PROOF')
        expect(page.locator('[data-source-action=next]')).to_be_disabled()
        page.locator('[data-source-action=tool_evidence]').click()
        expect(original).to_have_text(tool_jsonl[:6000])
        expect(page.locator('.evo-outcome-reader')).to_contain_text(re.compile('Earlier removed messages|更早已移除'))
        assert page.locator('.evo-outcome-reader script').count() == 0
        page.locator('[data-source-action=next]').click()
        expect(original).to_have_text(tool_jsonl[6000:12000])
        assert any('source_kind=tool_evidence' in request.url for request in source_reads)
        page.locator('[data-source-action=deliverable]').click()
        expect(original).to_have_text(content[:6000])
        assert all(request.method == 'GET' for request in source_reads)
        assert len(tasks) == 2 and not errors
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 2')
        artifact = Path('output/playwright') / f'stage-outcome-reader-{width}-{background}.png'
        artifact.parent.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(artifact))
        page.reload()
        expect(page.locator('.evo-mission-panel')).to_contain_text(run['objective'], timeout=20000)
        page.locator('.evo-ledger-head[data-studio-action=details]').click()
        page.locator(f'[data-outcome-task-id="{executor["id"]}"]').click()
        expect(page.locator('.evo-outcome-reader pre')).to_have_text(content[:6000])
        assert len(tasks) == 2
    finally:
        if handle:
            with commands._lock:commands._registry.pop(handle,None)
        context.close()


@pytest.mark.parametrize('width', [390, 1440])
@pytest.mark.parametrize('source_kind',['receipt','background_evidence'])
def test_semantic_history_opens_original_failed_evidence_without_new_consumption(browser, live_server, task_runtime, monkeypatch, width, source_kind):
    from playwright.sync_api import expect
    from tests.conftest import _dismiss_onboarding_modals
    from lib.evolution import programs, semantic_memory
    from lib.evolution.exploration_tools import execute
    from lib.agent_core.execution_budget import model_budget_admission
    runs, _, tasks = task_runtime
    ranked_queries = []
    def rank(parent, query, chunks):
        ranked_queries.append(query)
        lease = model_budget_admission(parent)(input_tokens=20, max_output_tokens=20)
        lease.mark_dispatched();lease.settle({'prompt_tokens': 20, 'completion_tokens': 10})
        chunk = next(chunk for chunk in chunks if 'network saturated' in chunk['text'])
        return {'matches': [{'candidate_id': chunk['candidate_id'], 'relevance': .9,
                            'quote': 'network saturated', 'reason': 'Synthetic relevance judgment for browser plumbing.'}],
                'actuals': {'tokens': 30, 'tool_calls': 0, 'wall_clock_seconds': 1},
                'actuals_complete': True, 'task_id': 'fixture-rank', 'inspector_task_id': 'fixture-rank',
                'model': 'synthetic-browser-fixture', 'received_events': 2}
    monkeypatch.setattr(semantic_memory, '_rank', rank)
    context = browser.new_context(viewport={'width': width, 'height': 960})
    context.add_init_script("localStorage.setItem('tofu_onboarding_v1_done','1')")
    page = context.new_page(); errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    try:
        page.goto(live_server + '/#/evolution');_dismiss_onboarding_modals(page)
        expect(page.locator('.exp-create-form')).to_be_visible(timeout=20000)
        page.locator('[name=objective]').fill('Inspect older failures before inventing a transfer mechanism.')
        with page.expect_response(lambda response: '/programs/' in response.url and response.url.endswith('/commands')):
            page.locator('.exp-create-form button[type=submit]').click()
        expect(page.locator('.exp-detail')).to_be_visible(timeout=20000)
        pid = page.url.split('/programs/')[1];owner = tasks[0]['_userId']
        for _ in range(3):
            executor = tasks[-1]
            executor.update(status='done', finishReason='stop', content='Measured network saturated; adding processors did not increase throughput.',
                            finished_at=executor['created_at'] + 1, usage={'input_tokens': 10, 'output_tokens': 5})
            if source_kind=='background_evidence':
                import threading
                from lib.tasks_pkg import background_commands as commands
                from lib.project_mod.run_command import _format_run_output
                run=runs.get_run(executor['config']['evolution']['run_id'],user_id=owner)
                run['require_command_execution']=True
                runs.records.put_record(runs.records.NAMESPACE_RUNS,runs.records.entity_key(run['run_id'],owner,kind='run'),run,kind='run')
                handle='bg_'+uuid.uuid4().hex[:12]
                executor['toolRounds']=[{'toolName':'run_command','toolCallId':'launch','status':'done','results':[{'exitCode':'background','backgroundCommandId':handle}]}]
                assert commands.register_handed_off_command(command_id=handle,task=executor,command='experiment',done=threading.Event())
                try:
                    waiting=runs.advance_run(run['run_id'],user_id=owner,terminal_task=executor)
                    assert waiting['background_wait']
                    commands.finish_handed_off_command(command_id=handle,result=_format_run_output(
                        'experiment','前段实验。'*7000+' network saturated','',0),error=None)
                    reviewing=runs.advance_run(run['run_id'],user_id=owner)
                    assert reviewing['status']=='reviewing',(reviewing['status'],reviewing['error'])
                finally:
                    with commands._lock:commands._registry.pop(handle,None)
            else:
                reviewing = runs.advance_run(executor['config']['evolution']['run_id'], user_id=owner, terminal_task=executor)
            review = {'evidence_audit': {'complete': True, 'objections': []}, 'criterion_results': [{'criterion_id': key, 'outcome': 'fail', 'evidence_ref': 'Synthetic measured counterexample.'} for key in runs.RUBRIC],
                      'requirement_results': [{'requirement_id': f'requirement-{i+1}', 'outcome': 'fail', 'evidence_ref': 'Synthetic baseline failure.'}
                                              for i in range(len(reviewing['acceptance_requirements']))]}
            reviewer = tasks[-1]
            if source_kind=='background_evidence':
                from tests.evolution.test_evolution_source_tools import delivered
                for field,kind in [('result_source','deliverable'),('tool_evidence_source','tool_evidence'),('background_evidence_source','background_evidence')]:
                    reference=reviewing.get(field)
                    if not reference:continue
                    offset=0
                    while True:
                        original=delivered(reviewer,{'action':'read','run_id':reviewing['run_id'],'task_id':reference['task_id'],'source_sha256':reference['source_sha256'],'source_kind':kind,'offset':offset},call_id=kind+str(offset))
                        if original['next_offset'] is None:break
                        offset=original['next_offset']
            reviewer.update(status='done', finishReason='stop', content=json.dumps(review), finished_at=reviewer['created_at'] + 1,
                            usage={'input_tokens': 10, 'output_tokens': 5})
            runs.advance_run(reviewing['run_id'], user_id=owner, terminal_task=reviewer)
        settled=programs.advance_program(pid, user_id=owner)
        assert settled['status']=='active',(settled['status'],settled['error'],runs.get_run(reviewing['run_id'],user_id=owner).get('review'))
        programs.advance_program(pid, user_id=owner)
        programs.advance_program(pid, user_id=owner)
        parent = tasks[-1]
        execute(parent, {'program_id': pid, 'action': 'semantic_recall', 'query': '带宽瓶颈的旧反例', **({'source_kind':source_kind,'offset':24000} if source_kind=='background_evidence' else {})}, call_id='browser-recall')
        count = len(tasks);consumption = parent['_executionBudget'].consumption()
        page.reload()
        history = page.locator('[data-semantic-memory]');detail = history.locator('details')
        expect(detail).to_have_count(1, timeout=20000);detail.locator('summary').click()
        expect(detail).to_contain_text('network saturated');expect(detail).to_contain_text('30')
        if source_kind=='background_evidence':
            original=detail.locator('[data-program-source=background_evidence]')
            expect(original).to_be_visible()
            with page.expect_response(lambda response:'source_kind=background_evidence' in response.url):original.click()
            expect(page.locator('.evo-outcome-reader')).to_contain_text('network saturated')
            page.locator('.evo-outcome-reader [data-source-action=close]').click()
        source = detail.locator('button').first;source.focus()
        with page.expect_response(lambda response: response.url.endswith('/memory')) as refreshed:
            page.evaluate("document.dispatchEvent(new Event('visibilitychange'))")
        refreshed.value.finished()
        page.evaluate('() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))')
        expect(source).to_be_focused();expect(detail).to_have_attribute('open', '')
        with page.expect_response(lambda response: '/experiments?' in response.url):
            source.click()
        receipt = page.locator('.exp-receipt').first;receipt.locator('summary').click()
        expect(receipt).to_contain_text('network saturated');expect(receipt).to_contain_text('failure')
        page.reload();expect(history.locator('details')).to_have_count(1, timeout=20000)
        assert parent['_executionBudget'].consumption() == consumption
        assert len(tasks) == count and len(ranked_queries) == 1 and not errors
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        output = Path('output/playwright');output.mkdir(parents=True, exist_ok=True)
        history.locator('summary').click()
        page.screenshot(path=str(output / f'evolution-semantic-memory-{width}-{source_kind}.png'), full_page=True)
    finally:
        context.close()


@pytest.mark.parametrize('width', [390, 1440])
@pytest.mark.parametrize('runtime_name', ['task_runtime', 'stored_program_runtime'])
def test_executor_invention_is_visible_durable_and_not_a_success_claim(browser, live_server, request, runtime_name, width):
    from playwright.sync_api import expect
    from tests.conftest import _dismiss_onboarding_modals
    from lib.evolution import programs
    from lib.evolution.exploration_tools import execute
    from tests.evolution.test_evolution_exploration_tools import candidate, queued_candidate
    _, _, tasks = request.getfixturevalue(runtime_name)
    proposal = candidate()
    context = browser.new_context(viewport={'width': width, 'height': 960})
    context.add_init_script("localStorage.setItem('tofu_onboarding_v1_done','1')")
    page = context.new_page(); errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    try:
        page.goto(live_server + '/#/evolution')
        _dismiss_onboarding_modals(page)
        expect(page.locator('.exp-create-form')).to_be_visible(timeout=20000)
        page.locator('[name=objective]').fill('Discover unmet needs in interrupted research.')
        with page.expect_response(lambda response: '/programs/' in response.url and response.url.endswith('/commands')):
            page.locator('.exp-create-form button[type=submit]').click()
        expect(page.locator('.exp-detail')).to_be_visible(timeout=20000)
        owner = tasks[0]['_userId']
        program = programs.get_program(page.url.split('/programs/')[1], user_id=owner)
        result = execute(tasks[0], {'program_id': program['program_id'], 'action': 'propose',
                                  'step': proposal}, call_id='new-hypothesis')
        assert 'unverified' in result['evidence_status']
        page.reload()
        hypothesis = page.locator('.exp-frontier .exp-hypothesis')
        expect(hypothesis).to_have_count(1, timeout=20000)
        hypothesis.locator('summary').click()
        expect(hypothesis).to_contain_text(proposal['hypothesis']['falsifier'])
        expect(hypothesis).to_contain_text(proposal['hypothesis']['expected_value'])
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        page.reload()
        expect(page.locator('.exp-frontier .exp-hypothesis')).to_have_count(1, timeout=20000)
        page.locator('[data-program-action=pause]').click()
        expect(page.locator('[data-program-action=start]')).to_be_visible()
        saved = programs.get_program(program['program_id'], user_id=owner)
        assert saved['status'] == 'paused' and saved['settled_through'] == 0
        assert saved['queue'][-2] == queued_candidate() and saved['queue'][-1]['kind'] == 'final'
        assert len(tasks) == 1 and not errors
    finally:
        context.close()


@pytest.mark.parametrize('width', [390, 1440])
@pytest.mark.parametrize('runtime_name', ['task_runtime', 'stored_program_runtime'])
def test_program_notebook_real_create_pause_resume_review_and_reload(browser, live_server, request, runtime_name, tmp_path, width):
    from playwright.sync_api import expect
    from tests.conftest import _dismiss_onboarding_modals
    from lib.evolution import programs, get_episode
    runs, _, tasks = request.getfixturevalue(runtime_name)
    context = browser.new_context(viewport={'width': width, 'height': 960})
    context.add_init_script("localStorage.setItem('tofu_onboarding_v1_done','1')")
    page = context.new_page()
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    try:
        page.goto(live_server + '/#/evolution')
        _dismiss_onboarding_modals(page)
        expect(page.locator('.exp-create-form')).to_be_visible(timeout=20000)
        page.locator('[name=objective]').fill('Explain binary search and retain restart evidence.')
        page.locator('[name=acceptance]').fill('Explain why each comparison halves the interval.')
        page.locator('[name=project]').fill(str(tmp_path))
        with page.expect_response(lambda response: '/programs/' in response.url and response.url.endswith('/commands')) as started:
            page.locator('.exp-create-form button[type=submit]').click()
        assert started.value.status == 200
        expect(page.locator('.exp-detail')).to_be_visible(timeout=20000)
        pid = page.url.split('/programs/')[1]
        assert len(tasks) == 1
        owner = tasks[0]['_userId']
        header = programs.get_program(pid, user_id=owner)
        assert header['status'] == 'active' and header['current']
        assert tasks[0]['config']['projectPath'] == str(tmp_path)
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        assert page.locator('.exp-main').evaluate('el => el.scrollWidth <= el.clientWidth + 1')
        # Pause is durable admission control; current execution remains available.
        page.locator('[data-program-action=pause]').click()
        expect(page.locator('[data-program-action=start]')).to_be_visible()
        assert len(tasks) == 1 and programs.get_program(pid, user_id=owner)['status'] == 'paused'
        page.reload()
        expect(page.locator('[data-program-action=start]')).to_be_visible(timeout=20000)
        page.locator('[data-program-action=start]').click()
        expect(page.locator('[data-program-action=pause]')).to_be_visible()
        assert len(tasks) == 1
        # Advance the real durable service exactly as the serving-loop sweep does.
        for _ in range(6):
            header = programs.get_program(pid, user_id=owner)
            rid = header['current']['run_id']
            run = runs.get_run(rid, user_id=owner)
            executor = next(task for task in tasks if task['id'] == run['task_id'])
            if run.get('require_command_execution'):
                executor['toolRounds'] = [{'toolName':'run_command','toolCallId':'synthetic-command','status':'done','results':[{'exitCode':'0'}]}]
                executor['messages'].append({'role':'tool','tool_call_id':'synthetic-command','content':'Synthetic command observation for workflow verification.'})
            executor.update(status='done', finishReason='stop', content='Every comparison halves the interval. Observed test evidence.',
                            finished_at=executor['created_at'] + 1, usage={'input_tokens': 10, 'output_tokens': 5})
            reviewing = runs.advance_run(rid, user_id=owner, terminal_task=executor)
            reviewer = next(task for task in tasks if task['id'] == reviewing['task_id'])
            review = {'evidence_audit': {'complete': True, 'objections': []}, 'criterion_results': [{'criterion_id': key, 'outcome': 'pass', 'evidence_ref': 'Observed explanation and protocol.'} for key in runs.RUBRIC],
                      'requirement_results': [{'requirement_id': f'requirement-{i+1}', 'outcome': 'pass', 'evidence_ref': 'Observed explanation and protocol.'}
                                              for i in range(len(reviewing['acceptance_requirements']))]}
            reviewer.update(status='done', finishReason='stop', content=json.dumps(review), finished_at=reviewer['created_at'] + 1,
                            usage={'input_tokens': 10, 'output_tokens': 5})
            settled = runs.advance_run(rid, user_id=owner, terminal_task=reviewer)
            assert settled['status'] == 'completed'
            assert get_episode(settled['episode_id'], user_id=owner)['status'] == 'success'
            header = programs.advance_program(pid, user_id=owner)
            if header['status'] != 'completed':
                programs.advance_program(pid, user_id=owner)
                programs.advance_program(pid, user_id=owner)
        assert header['status'] == 'completed' and header['settled_through'] == 6
        page.locator('.exp-actions button').first.click()
        expect(page.locator('.exp-receipt')).to_have_count(6, timeout=20000)
        expect(page.locator('.exp-output')).to_contain_text('Every comparison halves the interval.')
        page.locator('.exp-receipt summary').last.click()
        expect(page.locator('.exp-receipt').last).to_contain_text('Explain why each comparison halves the interval.')
        page.reload()
        expect(page.locator('.exp-receipt')).to_have_count(6, timeout=20000)
        expect(page.locator('.exp-output')).to_contain_text('Every comparison halves the interval.')
        assert page.locator('[data-program-action=start]').is_hidden()
        output = Path('output/playwright'); output.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(output / f'evolution-program-delivery-{width}.png'), full_page=True)
        assert not errors
        if runtime_name == 'stored_program_runtime':
            from lib.storage.service import storage_runtime
            from lib.storage import storage_status
            frozen = programs.get_program(pid, user_id=owner)
            frozen_history = programs.experiment_page(pid, user_id=owner)
            frozen_runs = [runs.get_run(row['run_id'], user_id=owner) for row in frozen_history['experiments']]
            count = len(tasks)
            authority = storage_runtime()
            authority.stop(); authority.start()
            assert programs.get_program(pid, user_id=owner) == frozen
            assert programs.experiment_page(pid, user_id=owner) == frozen_history
            assert [runs.get_run(row['run_id'], user_id=owner) for row in frozen_history['experiments']] == frozen_runs
            assert len(tasks) == count
            evidence = {'width': width, 'authority': storage_status(), 'program_id': pid,
                'settled_through': frozen['settled_through'], 'exact_header_history_and_runs_after_engine_restart': True,
                'task_births_before_and_after': count, 'models': 0,
                'scope': 'Actual HTTP/browser/semantic storage and owned engine restart. Model execution and review output are controlled inputs; no demand/value proof.'}
            (output / f'evolution-stored-program-{width}.json').write_text(json.dumps(evidence, indent=2))
    finally:
        context.close()


@pytest.mark.parametrize('width', [390, 1440])
@pytest.mark.parametrize('runtime_name', ['task_runtime', 'stored_program_runtime'])
def test_failure_diagnosis_proposal_review_and_retained_checks_survive_reload(browser, live_server, request, runtime_name, width):
    from playwright.sync_api import expect
    from tests.conftest import _dismiss_onboarding_modals
    from tests.evolution.test_evolution_exploration_tools import candidate
    from lib.evolution import programs
    from lib.evolution.exploration_tools import execute
    runs, _, tasks = request.getfixturevalue(runtime_name)
    context = browser.new_context(viewport={'width': width, 'height': 960})
    context.add_init_script("localStorage.setItem('tofu_onboarding_v1_done','1')")
    page = context.new_page(); errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    try:
        page.goto(live_server + '/#/evolution'); _dismiss_onboarding_modals(page)
        expect(page.locator('.exp-create-form')).to_be_visible(timeout=20000)
        assert page.locator('[name=auto-replan]').input_value() == 'auto'
        page.locator('[name=objective]').fill('Find a new mechanism for interrupted research handoffs.')
        with page.expect_response(lambda response: '/programs/' in response.url and response.url.endswith('/commands')):
            page.locator('.exp-create-form button[type=submit]').click()
        expect(page.locator('.exp-detail')).to_be_visible(timeout=20000)
        owner = tasks[0]['_userId']; pid = page.url.split('/programs/')[1]
        initial = programs.get_program(pid, user_id=owner)
        def finish_task(_runtime, task, content):
            task.update(status='done', finishReason='stop', content=content, finished_at=task['created_at'] + 1,
                        usage={'input_tokens': 10, 'output_tokens': 5})
            return runs.advance_run(task['config']['evolution']['run_id'],
                                    user_id=owner, terminal_task=task)
        def review_for(run, outcome):
            return json.dumps({'evidence_audit': {'complete': True, 'objections': []}, 'criterion_results': [{'criterion_id': key, 'outcome': outcome,
                'evidence_ref': 'Observed concurrent handoff trace.'} for key in runs.RUBRIC],
                'requirement_results': [{'requirement_id': f'requirement-{index+1}', 'outcome': outcome,
                    'evidence_ref': 'Observed requirement reproduction.'}
                    for index in range(len(run['acceptance_requirements']))]})
        for _ in range(3):
            reviewed = finish_task(None, tasks[-1], 'Observed baseline failed to preserve concurrent handoff evidence.')
            finish_task(None, tasks[-1], review_for(reviewed, 'fail'))
        failed = programs.advance_program(pid, user_id=owner)
        assert failed['queue'][0]['kind'] == 'replan'
        programs.advance_program(pid, user_id=owner)
        current = programs.advance_program(pid, user_id=owner)
        proposal = candidate()
        execute(tasks[-1], {'program_id': pid, 'action': 'replan',
            'reason': 'The observed trace disproves the serial handoff assumption.', 'steps': [proposal]}, call_id='browser-plan')
        page.reload()
        expect(page.locator('.exp-current .exp-replan')).to_have_count(1, timeout=20000)
        page.locator('.exp-current .exp-replan > summary').click()
        expect(page.locator('.exp-current .exp-replan')).to_contain_text('serial handoff assumption')
        assert len(programs.get_program(pid, user_id=owner)['queue']) == len(initial['queue']) + 1
        page.locator('[data-program-action=pause]').click()
        expect(page.locator('[data-program-action=start]')).to_be_visible()
        reviewed = finish_task(None, tasks[-1], 'Inspected failure and submitted falsifiable alternatives.')
        finish_task(None, tasks[-1], review_for(reviewed, 'pass'))
        settled = programs.advance_program(pid, user_id=owner)
        assert settled['status'] == 'paused'
        assert settled['queue'][1:] == initial['queue']
        assert settled['queue'][0]['hypothesis'] == proposal['hypothesis']
        page.reload()
        expect(page.locator('.exp-frontier .exp-hypothesis')).to_have_count(1, timeout=20000)
        archived = page.locator('.exp-receipt[data-sequence="2"]')
        archived.locator('> summary').click()
        expect(archived.locator('.exp-replan')).to_have_count(1)
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        assert not errors and len(tasks) == 8
        assert programs.get_program(pid, user_id=owner)['status'] != 'completed'
    finally:
        context.close()


@pytest.mark.parametrize('width', [390, 1440])
def test_recovered_review_keeps_partial_cost_visible_after_program_settlement(browser, live_server, task_runtime, monkeypatch, width):
    from playwright.sync_api import expect
    from tests.conftest import _dismiss_onboarding_modals
    from lib.evolution import programs, review_recovery
    runs, _, tasks = task_runtime
    context = browser.new_context(viewport={'width': width, 'height': 960})
    context.add_init_script("localStorage.setItem('tofu_onboarding_v1_done','1')")
    page = context.new_page(); errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    try:
        page.goto(live_server + '/#/evolution');_dismiss_onboarding_modals(page)
        expect(page.locator('.exp-create-form')).to_be_visible(timeout=20000)
        page.locator('[name=objective]').fill('Explore restart-safe evidence handoffs.')
        with page.expect_response(lambda response: '/programs/' in response.url and response.url.endswith('/commands')):
            page.locator('.exp-create-form button[type=submit]').click()
        expect(page.locator('.exp-detail')).to_be_visible(timeout=20000)
        owner = tasks[0]['_userId'];pid = page.url.split('/programs/')[1]
        run_id = tasks[0]['config']['evolution']['run_id']
        executor = tasks[0]
        executor.update(status='done',finishReason='stop',content='Observed handoff traces and concrete acceptance protocol.',
                        finished_at=executor['created_at']+1,usage={'input_tokens':10,'output_tokens':5})
        reviewing = runs.advance_run(run_id,user_id=owner,terminal_task=executor)
        lost_id = reviewing['task_id'];original_loader = runs.default_task_loader
        monkeypatch.setattr(runs,'default_task_loader',lambda tid,user_id:None if tid==lost_id else original_loader(tid,user_id=user_id))
        monkeypatch.setattr(review_recovery,'owner_state',lambda owner:'dead')
        prepared = runs.advance_run(run_id,user_id=owner)
        assert prepared['status']=='preparing' and prepared['actuals_complete'] is False
        page.reload()
        expect(page.locator('.exp-current')).to_contain_text(re.compile('observed lower bounds|已观测下界'),timeout=20000)
        recovered = runs.advance_run(run_id,user_id=owner)
        assert recovered['status']=='reviewing' and len(tasks)==3
        reviewer = tasks[-1]
        assessment = {'evidence_audit': {'complete': True, 'objections': []}, 'criterion_results':[{'criterion_id':key,'outcome':'pass','evidence_ref':'Observed handoff protocol.'} for key in runs.RUBRIC],
            'requirement_results':[{'requirement_id':f'requirement-{i+1}','outcome':'pass','evidence_ref':'Observed protocol and tool evidence.'} for i in range(len(recovered['acceptance_requirements']))]}
        reviewer.update(status='done',finishReason='stop',content=json.dumps(assessment),finished_at=reviewer['created_at']+1,usage={'input_tokens':10,'output_tokens':5})
        runs.advance_run(run_id,user_id=owner,terminal_task=reviewer)
        settled = programs.advance_program(pid,user_id=owner)
        assert settled['actuals_complete'] is False and settled['actuals']['tokens']==30
        receipt = programs.experiment_page(pid,user_id=owner)['experiments'][0]
        assert receipt['actuals_complete'] is False
        page.reload()
        expect(page.locator('.exp-telemetry')).to_contain_text('≥ 30',timeout=20000)
        expect(page.locator('.exp-telemetry')).to_contain_text(re.compile('not complete cost measurements|不能作为完整成本测量'))
        row = page.locator('.exp-receipt[data-sequence="1"]');row.locator('> summary').click()
        expect(row).to_contain_text(re.compile('observed lower bounds|已观测下界'))
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        assert not errors and len(tasks)==3
    finally:
        context.close()


@pytest.mark.parametrize('width,outcome', [(390, 'pass'), (1440, 'fail')])
def test_invalid_review_retry_preserves_original_output_cost_and_decision_after_reload(browser, live_server, task_runtime, width, outcome):
    """Real owner HTTP and built UI; provider output is explicitly synthetic."""
    from playwright.sync_api import expect
    from tests.conftest import _dismiss_onboarding_modals
    runs, _, tasks = task_runtime
    context = browser.new_context(viewport={'width': width, 'height': 960})
    context.add_init_script("localStorage.setItem('tofu_onboarding_v1_done','1')")
    page = context.new_page();errors=[]
    page.on('pageerror', lambda error: errors.append(str(error)))
    try:
        page.goto(live_server+'/#/evolution/runs',wait_until='domcontentloaded')
        _dismiss_onboarding_modals(page)
        expect(page.locator('.evo-studio')).to_be_visible(timeout=20000)
        response=page.evaluate("""async()=>{const r=await fetch('/api/v4/evolution/runs',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({objective:'Inspect a synthetic format failure without repeating execution.',max_attempts:1,max_review_format_retries:1})});return {status:r.status,body:await r.json()};}""")
        assert response['status']==201
        run=response['body']['data']['run'];executor=next(t for t in tasks if t['id']==run['task_id']);owner=executor['_userId']
        def terminal(task, content):
            task.update(status='done',finishReason='stop',content=content,finished_at=task['created_at']+1,usage={'input_tokens':10,'output_tokens':5})
            return runs.advance_run(run['run_id'],user_id=owner,terminal_task=task)
        reviewing=terminal(executor,'Synthetic inspected delivery.')
        invalid=next(t for t in tasks if t['id']==reviewing['task_id'])
        assessment=json.dumps({'evidence_audit': {'complete': True, 'objections': []}, 'criterion_results':[{'criterion_id':key,'outcome':outcome,'evidence_ref':'Synthetic controlled assessment.'} for key in runs.RUBRIC]})
        # Complete prose-wrapped judgments are supported lossless input.
        # An unfinished object remains an actual format failure to recover.
        raw='SYNTHETIC_FORMAT_FAILURE <script>throw 1</script>\n{"criterion_results":'
        retry=terminal(invalid,raw)
        assert retry['status']=='reviewing' and retry['attempt']==1 and len(tasks)==3
        successor=next(t for t in tasks if t['id']==retry['task_id'])
        settled=terminal(successor,assessment)
        assert settled['status']==('completed' if outcome=='pass' else 'needs_attention')
        assert settled['actuals']['tokens']==45 and len(settled['receipts'])==3
        page.goto(live_server+'/#/evolution/runs/'+run['run_id'],wait_until='domcontentloaded')
        expect(page.locator('.evo-mission-panel')).to_contain_text(run['objective'],timeout=20000)
        for reload in (False,True):
            if reload:
                page.reload();expect(page.locator('.evo-mission-panel')).to_contain_text(run['objective'],timeout=20000)
            page.locator('.evo-ledger-head[data-studio-action=details]').click()
            notice=page.locator(f'[data-review-format-retry="{invalid["id"]}"]')
            expect(notice).to_be_visible(timeout=20000)
            expect(notice).to_contain_text(re.compile('A new review was scheduled|已安排重新评审'))
            with page.expect_response(lambda response:'/outcomes/' in response.url):
                page.locator(f'[data-outcome-task-id="{invalid["id"]}"]').click()
            expect(page.locator('.evo-outcome-reader pre')).to_have_text(raw)
            assert page.locator('.evo-outcome-reader script').count()==0
        assert not errors and len(tasks)==3
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 2')
        artifact=Path('output/playwright')/f'review-format-retry-{width}.png';artifact.parent.mkdir(parents=True,exist_ok=True)
        page.screenshot(path=str(artifact))
    finally:
        context.close()


@pytest.mark.parametrize('width', [390, 1440])
def test_consecutive_creations_keep_failure_policy_in_real_storage(browser, live_server, stored_program_runtime, width):
    """Actual HTTP/UI/storage writes; pending executor births never run a model."""
    from playwright.sync_api import expect
    from tests.conftest import _dismiss_onboarding_modals
    from lib.evolution import programs
    _, _, tasks = stored_program_runtime
    context = browser.new_context(viewport={'width': width, 'height': 960})
    context.add_init_script("localStorage.setItem('tofu_onboarding_v1_done','1')")
    page = context.new_page(); errors = []; created = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    try:
        page.goto(live_server + '/#/evolution/programs/new'); _dismiss_onboarding_modals(page)
        expect(page.locator('.exp-create-form')).to_be_visible(timeout=20000)
        for index, policy in enumerate(['auto', 'auto', 'manual', 'manual']):
            if index == 2:
                page.locator('[name=auto-replan]').select_option('manual')
            assert page.locator('[name=auto-replan]').input_value() == policy
            page.locator('[name=objective]').fill(f'Controlled policy persistence journey {width} {index}')
            with page.expect_response(lambda response: response.url.endswith('/commands')) as started:
                page.locator('.exp-create-form button[type=submit]').click()
            assert started.value.status == 200
            expect(page.locator('.exp-detail')).to_be_visible(timeout=20000)
            pid = page.url.split('/programs/')[1]; owner = tasks[-1]['_userId']
            header = programs.get_program(pid, user_id=owner)
            assert header['auto_replan'] is (policy == 'auto')
            created.append({'program_id': pid, 'auto_replan': header['auto_replan'], 'owner': owner})
            with page.expect_response(lambda response: response.url.endswith('/commands')) as cancelled:
                page.locator('[data-program-action=cancel]').click()
            assert cancelled.value.status == 200
            assert programs.get_program(pid, user_id=owner)['status'] == 'cancelled'
            page.locator('.exp-sidebar > button').first.click()
            expect(page.locator('.exp-create-form')).to_be_visible()
            expect(page.locator('[name=objective]')).to_have_value('')
        assert [row['auto_replan'] for row in created] == [True, True, False, False]
        assert len(tasks) == 4 and not errors
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        artifact = Path('output/playwright') / f'studio-consecutive-policy-{width}.json'
        artifact.parent.mkdir(parents=True, exist_ok=True)
        artifact.write_text(json.dumps({'width': width, 'programs': created,
            'scope': 'Real owner records, HTTP and browser; synthetic pending task births, zero models'}, indent=2))
    finally:
        context.close()


@pytest.mark.parametrize('width', [390, 1440])
def test_lost_executor_tool_allowance_blocks_retry_and_keeps_cancel_available(browser, live_server, stored_program_runtime, monkeypatch, width):
    """Real HTTP/storage/rendering; controlled task loss, no model or process kill."""
    from copy import deepcopy
    from playwright.sync_api import expect
    from tests.conftest import _dismiss_onboarding_modals
    from lib.evolution import programs, review_recovery
    runs, _, tasks = stored_program_runtime
    context = browser.new_context(viewport={'width': width, 'height': 960})
    context.add_init_script("localStorage.setItem('tofu_onboarding_v1_done','1')")
    page = context.new_page(); errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    try:
        page.goto(live_server + '/#/evolution/programs/new'); _dismiss_onboarding_modals(page)
        expect(page.locator('.exp-create-form')).to_be_visible(timeout=20000)
        page.locator('.exp-create-form details.exp-execution-options summary').click()
        page.locator('[name=program-tool-limit]').fill('100')
        page.locator('[name=objective]').fill('Retain cumulative tools across a controlled executor loss.')
        with page.expect_response(lambda response: '/programs/' in response.url and response.url.endswith('/commands')):
            page.locator('.exp-create-form button[type=submit]').click()
        expect(page.locator('.exp-detail')).to_be_visible(timeout=20000)
        owner = tasks[0]['_userId']; pid = page.url.split('/programs/')[1]
        monkeypatch.setattr(runs, 'default_task_loader', lambda tid, user_id: None)
        monkeypatch.setattr(review_recovery, 'owner_state', lambda stamp: 'dead')
        stopped = programs.advance_program(pid, user_id=owner)
        assert stopped['status'] == 'needs_attention' and stopped['tool_calls_complete'] is False
        assert 'executor_outcome_unknown' in stopped['error']
        before = deepcopy(stopped)
        page.reload(); retry = page.locator('[data-program-action=retry]')
        expect(retry).to_be_visible(timeout=20000); expect(retry).to_be_disabled()
        reason = page.locator('.exp-attention [role=status]')
        expect(reason).to_be_visible(); assert reason.inner_text() and reason.inner_text() == retry.get_attribute('title')
        expect(page.locator('[data-program-action=cancel]')).to_be_enabled()
        response = page.request.post(live_server + '/api/v4/evolution/programs/' + pid + '/commands', data={'action': 'retry'})
        assert not response.ok and 'program tool consumption is unknown' in response.text()
        assert programs.get_program(pid, user_id=owner) == before and len(tasks) == 1
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth') and not errors
        page.locator('[data-program-action=cancel]').click()
        expect(retry).to_be_hidden()
        assert programs.get_program(pid, user_id=owner)['status'] == 'cancelled' and len(tasks) == 1
    finally:
        context.close()


@pytest.mark.parametrize('width', [390, 1440])
def test_prepare_inspect_and_change_pacing_without_execution(browser, live_server, monkeypatch, width):
    """Real HTTP/store journey: saving and inspecting are never launch authority."""
    from playwright.sync_api import expect
    from tests.conftest import _dismiss_onboarding_modals
    from lib.evolution import programs

    owner = 1000 + uuid.uuid4().int % 1_000_000_000
    monkeypatch.setattr('routes.api_v1.evolution.request_user_id', lambda: owner)
    monkeypatch.setattr(programs, '_enroll', lambda *args: pytest.fail('Preparation/pacing must not enroll execution'))
    # This isolated owner has no model-routing document; no model is needed to prepare.
    monkeypatch.setattr('lib.evolution.workspace.model_options', lambda **kw: [])
    context = browser.new_context(viewport={'width': width, 'height': 960})
    context.add_init_script("localStorage.setItem('tofu_onboarding_v1_done','1')")
    page = context.new_page(); errors = []; commands = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.on('request', lambda request: commands.append(request.post_data_json)
            if request.method == 'POST' and '/evolution/programs/' in request.url and request.url.endswith('/commands') else None)
    try:
        page.goto(live_server + '/#/evolution')
        _dismiss_onboarding_modals(page)
        page.locator('[name=objective]').fill('Prepare a durable multi-day experiment before spending')
        page.locator('[name=acceptance]').fill('Preserve evidence and compare independently against the original goal')
        page.locator('[name=advance-mode]').select_option('checkpoint')
        with page.expect_response(lambda response: response.request.method == 'POST'
                                  and response.url.endswith('/evolution/programs')) as created:
            page.locator('.exp-create-form button[type=button]').click()
        assert created.value.status == 201
        program = created.value.json()['data']['program']
        program_id = program['program_id']
        assert program['status'] == 'ready' and program['started_at'] is None
        assert program['advance_mode'] == 'checkpoint' and program['current'] is None
        expect(page).to_have_url(live_server + '/#/evolution/programs/' + program_id)
        expect(page.locator('[data-protocol]')).to_have_count(len(program['queue']))
        protocol = page.locator('[data-protocol]').first
        protocol.locator('summary').click()
        expect(protocol.locator('pre')).to_have_text(program['queue'][0]['instruction'])
        expect(protocol.locator('ul')).to_contain_text(program['queue'][0]['checks'][0])
        with page.expect_response(lambda response: response.url.endswith('/commands') and response.request.method == 'POST'):
            page.locator('[data-program-pacing]').click()
        page.wait_for_function("document.querySelector('[data-protocol]').open === true")
        assert commands == [{'action': 'set_pacing', 'advance_mode': 'continuous'}]
        saved = programs.get_program(program_id, user_id=owner)
        assert saved['status'] == 'ready' and saved['started_at'] is None and saved['current'] is None
        assert saved['advance_mode'] == 'continuous'
        page.reload()
        expect(page.locator('.exp-detail')).to_be_visible(timeout=20000)
        expect(page.locator('[data-program-pacing]')).to_be_visible()
        assert commands == [{'action': 'set_pacing', 'advance_mode': 'continuous'}]
        protocol = page.locator('[data-protocol]').first
        protocol.locator('summary').click()
        protocol.locator('summary').focus()
        with page.expect_response(lambda response: response.url.endswith('/evolution/programs/' + program_id)):
            page.evaluate("document.dispatchEvent(new Event('visibilitychange'))")
        expect(protocol).to_have_attribute('open', '')
        expect(protocol.locator('summary')).to_be_focused()
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1')
        screenshot = Path('output/playwright') / f'program-preparation-{width}.png'
        screenshot.parent.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(screenshot), full_page=True)
        assert not errors
    finally:
        context.close()


@pytest.mark.parametrize('width',[390,1440])
def test_durable_background_wait_survives_reload_then_opens_late_original(browser,live_server,task_runtime,monkeypatch,width):
    import threading
    import uuid
    from playwright.sync_api import expect
    from tests.conftest import _dismiss_onboarding_modals
    from lib.tasks_pkg import background_commands as commands
    from lib.evolution import stage_outcomes as outcomes
    runs,_,tasks=task_runtime
    context=browser.new_context(viewport={'width':width,'height':960})
    context.add_init_script("localStorage.setItem('tofu_onboarding_v1_done','1')")
    page=context.new_page();errors=[];page.on('pageerror',lambda error:errors.append(str(error)))
    handle=None
    try:
        page.goto(live_server+'/#/evolution/runs',wait_until='domcontentloaded');_dismiss_onboarding_modals(page)
        expect(page.locator('.evo-studio')).to_be_visible(timeout=20000)
        created=page.evaluate("""async()=>{const response=await fetch('/api/v4/evolution/runs',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({objective:'Reproduce the frozen background comparison',require_command_execution:true,max_attempts:1})});return {status:response.status,body:await response.json()};}""")
        assert created['status']==201
        run=created['body']['data']['run'];executor=tasks[-1];owner=executor['_userId']
        handle='bg_'+uuid.uuid4().hex[:12]
        assert commands.register_handed_off_command(command_id=handle,task=executor,command='experiment',done=threading.Event())
        executor['toolRounds']=[{'toolName':'run_command','toolCallId':'launch','status':'done','results':[{'exitCode':'background','backgroundCommandId':handle}]}]
        executor.update(status='done',finishReason='stop',content='Experiment submitted; results are pending.',usage={'input_tokens':10,'output_tokens':5},finished_at=executor['created_at']+1)
        outcomes.capture(run,executor,user_id=owner,review_prompt_factory=lambda text:runs._review_prompt({**run,'result':text},executor))
        waiting=runs.advance_run(run['run_id'],user_id=owner,terminal_task=executor)
        assert waiting['background_wait'] and len(tasks)==1
        page.goto(live_server+'/#/evolution/runs/'+run['run_id'])
        expect(page.locator('.evo-mission-panel')).to_contain_text(run['objective'],timeout=20000)
        expect(page.locator('.evo-mission-panel')).to_contain_text(__import__('re').compile('Waiting for background experiments|正在等待后台实验'))
        page.reload();expect(page.locator('.evo-mission-panel')).to_contain_text(__import__('re').compile('Waiting for background experiments|正在等待后台实验'),timeout=20000)
        raw='$ experiment\n'+('measured paired data 🧪 <script>throw 1</script>\n'*250)+'LATE_ORIGINAL_TAIL\n[exit code: 0]'
        commands.finish_handed_off_command(command_id=handle,result=raw,error=None)
        resumed=runs.advance_run(run['run_id'],user_id=owner)
        assert resumed['status']=='reviewing' and len(tasks)==2
        with commands._lock:commands._registry.pop(handle,None)
        page.reload();expect(page.locator('.evo-mission-panel')).to_contain_text(run['objective'],timeout=20000)
        page.locator('.evo-ledger-head[data-studio-action=details]').click()
        page.locator(f'[data-outcome-task-id="{executor["id"]}"]').click()
        button=page.locator('[data-source-action=background_evidence]');expect(button).to_be_visible(timeout=20000);button.click()
        original=page.locator('.evo-outcome-reader pre');expect(original).to_contain_text('measured paired data')
        while not page.locator('[data-source-action=next]').is_disabled():page.locator('[data-source-action=next]').click();page.wait_for_timeout(100)
        expect(original).to_contain_text('LATE_ORIGINAL_TAIL')
        assert page.locator('.evo-outcome-reader script').count()==0
        assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+2') and not errors and len(tasks)==2
    finally:
        if handle:
            with commands._lock:commands._registry.pop(handle,None)
        context.close()


@pytest.mark.parametrize('width', [390, 1440])
def test_current_experiment_failure_is_readable_and_escaped(browser, live_server, stored_program_runtime, width):
    """Real shell/HTTP/program identity with a controlled run-error projection."""
    from playwright.sync_api import expect
    from tests.conftest import _dismiss_onboarding_modals
    context = browser.new_context(viewport={'width': width, 'height': 960})
    context.add_init_script("localStorage.setItem('tofu_onboarding_v1_done','1')")
    page = context.new_page()
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    try:
        created = page.request.post(live_server + '/api/v4/evolution/programs',
                                    headers={'Idempotency-Key': uuid.uuid4().hex}, data={
            'objective': 'Inspect the current experiment failure ' + uuid.uuid4().hex,
            'steps': [{'kind': 'final', 'title': 'Current failure inspection',
                       'instruction': 'Inspect evidence.', 'checks': ['Inspect originals.']}],
        })
        assert created.status == 201, created.text()
        pid = created.json()['data']['program']['program_id']
        started = page.request.post(live_server + f'/api/v4/evolution/programs/{pid}/commands',
                                    data={'action': 'start'})
        assert started.status == 200, started.text()
        run_id = started.json()['data']['program']['current']['run_id']
        reason = ['Provider stopped before a complete delivery.']
        def project_error(route):
            response = route.fetch()
            body = response.json()
            body['data']['run'].update(status='failed', error=reason[0])
            route.fulfill(response=response, json=body)
        page.route('**/api/v4/evolution/runs/' + run_id, project_error)
        page.goto(live_server + '/#/evolution/programs/' + pid)
        _dismiss_onboarding_modals(page)
        detail = page.locator('.exp-current-error')
        expect(detail).to_be_visible(timeout=20000)
        detail.locator('summary').click()
        expect(detail.locator('pre')).to_have_text(reason[0])
        reason[0] = '<img src=x onerror=alert(1)> Updated failure ' + 'reason ' * 100
        page.reload()
        expect(detail).to_be_visible(timeout=20000)
        detail.locator('summary').click()
        expect(detail.locator('pre')).to_have_text(reason[0])
        assert detail.locator('img').count() == 0
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1')
        shot = Path('output/playwright') / f'current-experiment-error-{width}.png'
        shot.parent.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(shot), full_page=True)
        assert not errors
    finally:
        context.close()
