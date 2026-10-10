"""Self-evolution API surface: intake → plan → confirm → launch → verdict."""
from __future__ import annotations

pytest_plugins = ('tests._credential_sidecar',)

import asyncio

import pytest

pytestmark = pytest.mark.unit

OWNER = 12_012


def test_program_catalog_http_pages_every_project_without_progressing_work(client):
    http, token, store = client
    from lib.evolution.programs import create_program
    from lib.evolution.contracts import validate_program
    ids = [create_program({'objective': f'Catalog exploration {index}'}, user_id=OWNER,
           idempotency_key=f'http-catalog-{index}')['program_id'] for index in range(53)]
    headers = {'Authorization': f'Bearer {token}'}
    seen = []
    cursor = ''
    writes = store.writes
    pages = []
    for _ in range(4):
        response = _run(http.get('/api/v4/evolution/programs?after='+cursor, headers=headers))
        assert response.status_code == 200
        page = _run(response.get_json())
        validate_program({key:value for key,value in page.items() if key != 'ok'}, 'catalog_page')
        pages.append(page); seen.extend(row['program_id'] for row in page['programs'])
        if page['next_cursor'] is None:
            break
        cursor = page['next_cursor']
    assert seen == sorted(ids) and len(pages) == 3
    backward = _run(http.get('/api/v4/evolution/programs?before='+pages[1]['previous_cursor'], headers=headers))
    assert _run(backward.get_json())['programs'] == pages[0]['programs']
    for query in ['limit=0', 'limit=51', 'limit=bad', 'after=a&before=b', 'after=wrong%3Aowner']:
        invalid = _run(http.get('/api/v4/evolution/programs?'+query, headers=headers))
        assert invalid.status_code == 400
    assert store.writes == writes


def test_program_memory_read_is_owner_scoped_and_does_not_launch_a_model(client, monkeypatch):
    http, token, _ = client
    headers = {'Authorization': f'Bearer {token}', 'Idempotency-Key': 'memory-read'}
    created = _run(http.post('/api/v4/evolution/programs', headers=headers,
                            json={'objective': 'Inspect long experiment evidence.'}))
    pid = _run(created.get_json())['program']['program_id']
    def unexpected(*args, **kwargs):
        pytest.fail('A history GET cannot launch semantic ranking')
    monkeypatch.setattr('lib.evolution.semantic_memory._rank', unexpected)
    response = _run(http.get(f'/api/v4/evolution/programs/{pid}/memory', headers=headers))
    assert response.status_code == 200
    memory = _run(response.get_json())['memory']
    assert memory['entries'] == [] and memory['retained_limit'] == 32
    from lib.evolution.contracts import validate_program
    validate_program(memory, 'semantic_history')
    from lib.api_keys import create_key
    _, other = create_key(owner_user_id=OWNER + 1, name='other-memory-owner', scopes=['tasks'])
    denied = _run(http.get(f'/api/v4/evolution/programs/{pid}/memory',
                          headers={'Authorization': f'Bearer {other}'}))
    absent = _run(http.get('/api/v4/evolution/programs/absent-program/memory',
                          headers={'Authorization': f'Bearer {other}'}))
    assert denied.status_code == absent.status_code == 409
    denied_body, absent_body = _run(denied.get_json()), _run(absent.get_json())
    assert denied_body['code'] == absent_body['code']
    assert denied_body['detail'] == absent_body['detail']


def test_durable_program_routes_preserve_idempotency_owner_controls_and_errors(client, monkeypatch):
    http, token, _ = client
    headers = {'Authorization': f'Bearer {token}', 'Idempotency-Key': 'program-api'}
    response = _run(http.post('/api/v4/evolution/programs', headers=headers,
                             json={'objective': 'Build a usable durable studio.'}))
    assert response.status_code == 201
    data = _run(response.get_json())
    program = data['program']
    pid = program['program_id']
    assert program['status'] == 'ready'
    assert [step['kind'] for step in program['queue']] == [
        'discovery', 'baseline', 'experiment', 'synthesis', 'experiment', 'final']
    validation = program['queue'][-2]
    assert validation['require_command_execution'] is True
    assert validation['requires_scientific_protocol'] is True
    assert validation['requires_frozen_measurement_scope'] is True
    assert program['auto_replan'] is True
    manual = _run(http.post('/api/v4/evolution/programs',
        headers={**headers, 'Idempotency-Key': 'manual-program-api'},
        json={'objective': 'Inspect causal failure manually.', 'auto_replan': False}))
    assert manual.status_code == 201
    assert _run(manual.get_json())['program']['auto_replan'] is False
    invalid_policy = _run(http.post('/api/v4/evolution/programs',
        headers={**headers, 'Idempotency-Key': 'invalid-policy-program-api'},
        json={'objective': 'Inspect causal failure.', 'auto_replan': 'yes'}))
    assert invalid_policy.status_code == 400
    duplicate = _run(http.post('/api/v4/evolution/programs', headers=headers,
                              json={'objective': program['objective']}))
    assert _run(duplicate.get_json())['program']['program_id'] == pid
    conflict = _run(http.post('/api/v4/evolution/programs', headers=headers,
                             json={'objective': 'A different objective'}))
    assert conflict.status_code == 409
    for action, expected in [('resume', 'active'), ('pause', 'paused'), ('cancel', 'cancelled')]:
        result = _run(http.post(f'/api/v4/evolution/programs/{pid}/commands',
                               headers=headers, json={'action': action}))
        assert result.status_code == 200
        assert _run(result.get_json())['program']['status'] == expected
    history = _run(http.get(f'/api/v4/evolution/programs/{pid}/experiments', headers=headers))
    assert _run(history.get_json())['experiments'] == []
    bad = _run(http.get(f'/api/v4/evolution/programs/{pid}/experiments?after=invalid', headers=headers))
    assert bad.status_code == 400
    missing_key = _run(http.post('/api/v4/evolution/programs',
        headers={'Authorization': f'Bearer {token}'}, json={'objective': 'Another objective'}))
    assert missing_key.status_code == 400
    monkeypatch.setattr('routes.api_v1.auth._mode_requires_credential', lambda: True)
    unauthorized = _run(http.get('/api/v4/evolution/programs', headers={'Authorization': 'Bearer invalid'}))
    assert unauthorized.status_code == 401


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


@pytest.fixture
def client(monkeypatch):
    from quart import Quart
    app = Quart(__name__, static_folder=None)
    app.config.setdefault('PROVIDE_AUTOMATIC_OPTIONS', True)
    app.config['TESTING'] = True

    from routes.api_v1.auth import (
        attach_rate_headers, bearer_auth_before_request,
    )
    app.before_request(bearer_auth_before_request)
    app.after_request(attach_rate_headers)

    from routes.api_v1.evolution import api_v1_evolution_bp
    app.register_blueprint(api_v1_evolution_bp)

    from tests.support.evolution_store import install_store
    store = install_store(monkeypatch)
    monkeypatch.setattr('lib.evolution.runs.resolve_run_model', lambda requested, **kw: requested)

    import lib.tasks_pkg.spawn as spawn_pkg

    def _fake_spawn(task):
        task['status'] = 'done'
        task['finishReason'] = 'stop'
        task['created_at'] = task.get('created_at') or 1.0
        task['finished_at'] = 30.0
        task['usage'] = {'input_tokens': 5, 'output_tokens': 3}
        task['toolRounds'] = [{'tool_calls': [{'name': 'x'}]}]

    monkeypatch.setattr(spawn_pkg, 'spawn_task', _fake_spawn)

    from lib.api_keys import create_key
    _row, token = create_key(owner_user_id=OWNER, name='evo-test',
                             scopes=['tasks'])
    return app.test_client(), token, store


def _goal_payload(**overrides):
    payload = {
        'schema_version': 1, 'goal_id': 'goal-api',
        'statement': 'make the bounded check pass', 'domain': 'code',
        'exploratory': False,
        'criteria': [{'criterion_id': 'c1', 'kind': 'explicit_human',
                      'params': {}}],
        'budget_envelope': {'max_tokens': 50000, 'max_wall_clock_seconds': 600,
                            'max_tool_calls': 20},
        'status': 'draft', 'created_at': 1,
    }
    payload.update(overrides)
    return payload


def _headers(token):
    return {'Authorization': f'Bearer {token}'}


def test_goal_intake_validates_the_contract(client):
    cli, token, _store = client
    async def go():
        bad = _goal_payload(criteria=[])
        response = await cli.post('/api/v4/evolution/goals', json=bad,
                                  headers=_headers(token))
        assert response.status_code == 400
        good = await cli.post('/api/v4/evolution/goals', json=_goal_payload(),
                              headers=_headers(token))
        assert good.status_code == 201
        body = await good.get_json()
        assert body['goal']['status'] == 'admitted'
        listing = await cli.get('/api/v4/evolution/goals',
                                headers=_headers(token))
        assert len((await listing.get_json())['goals']) == 1
    _run(go())


def test_plan_confirm_launch_and_human_verdict(client):
    cli, token, _store = client
    async def go():
        await cli.post('/api/v4/evolution/goals', json=_goal_payload(),
                       headers=_headers(token))
        plan = await cli.post('/api/v4/evolution/goals/goal-api/plans',
                              json={}, headers=_headers(token))
        assert plan.status_code == 201
        plan_body = (await plan.get_json())['plan']
        assert plan_body['status'] == 'proposed'
        assert plan_body['candidates'][0]['cost_estimate'][
            'priors_source'] == 'exploratory_flat'

        launch_early = await cli.post(
            f"/api/v4/evolution/plans/{plan_body['plan_id']}/launch",
            json={}, headers=_headers(token))
        assert launch_early.status_code == 409

        confirm = await cli.post(
            f"/api/v4/evolution/plans/{plan_body['plan_id']}/confirm",
            json={}, headers=_headers(token))
        assert confirm.status_code == 200

        launch = await cli.post(
            f"/api/v4/evolution/plans/{plan_body['plan_id']}/launch",
            json={}, headers=_headers(token))
        assert launch.status_code == 201
        launched = await launch.get_json()
        episode = launched['episode']
        assert episode['status'] == 'running'
        assert launched['task_id']

        # Human criterion → the terminal listener leaves it running; the
        # explicit verdict endpoint settles it from task receipts.
        verdict = await cli.post(
            f"/api/v4/evolution/episodes/{episode['episode_id']}/verdict",
            json={'outcome': 'success'}, headers=_headers(token))
        assert verdict.status_code == 200
        settled = (await verdict.get_json())['episode']
        assert settled['status'] == 'success'
        assert settled['verdict']['assessed_by'] == 'human'
        assert settled['actuals'] == {'tokens': 8, 'wall_clock_seconds': 0,
                                      'tool_calls': 1}
    _run(go())


def test_plan_reject_and_mutation_enable_composite(client):
    cli, token, _store = client
    async def go():
        await cli.post('/api/v4/evolution/goals', json=_goal_payload(
            goal_id='goal-r'), headers=_headers(token))
        plan = await cli.post('/api/v4/evolution/goals/goal-r/plans',
                              json={}, headers=_headers(token))
        plan_id = (await plan.get_json())['plan']['plan_id']
        rejected = await cli.post(
            f'/api/v4/evolution/plans/{plan_id}/reject', json={},
            headers=_headers(token))
        assert rejected.status_code == 200
        assert (await rejected.get_json())['plan']['status'] == 'rejected'
        again = await cli.post(f'/api/v4/evolution/plans/{plan_id}/reject',
                               json={}, headers=_headers(token))
        assert again.status_code == 409

        # enable composite: confirm → regression → activate in one journey
        launch_goal = await cli.post('/api/v4/evolution/goals',
                                     json=_goal_payload(goal_id='goal-e'),
                                     headers=_headers(token))
        assert launch_goal.status_code == 201
        plan2 = await cli.post('/api/v4/evolution/goals/goal-e/plans',
                               json={}, headers=_headers(token))
        plan2_id = (await plan2.get_json())['plan']['plan_id']
        await cli.post(f'/api/v4/evolution/plans/{plan2_id}/confirm',
                       json={}, headers=_headers(token))
        launch = await cli.post(f'/api/v4/evolution/plans/{plan2_id}/launch',
                                json={}, headers=_headers(token))
        episode = (await launch.get_json())['episode']
        await cli.post(
            f"/api/v4/evolution/episodes/{episode['episode_id']}/verdict",
            json={'outcome': 'failure'}, headers=_headers(token))
        proposed = await cli.post('/api/v4/evolution/mutations', json={
            'kind': 'routing_change', 'payload': {'model': 'x'},
            'evidence_episode_ids': [episode['episode_id']]},
            headers=_headers(token))
        mutation = (await proposed.get_json())['mutation']
        enabled = await cli.post(
            f"/api/v4/evolution/mutations/{mutation['mutation_id']}/enable",
            json={}, headers=_headers(token))
        assert enabled.status_code == 200
        body = await enabled.get_json()
        assert body['final_status'] in ('needs_waiver', 'active')
        if body['final_status'] == 'needs_waiver':
            waived = await cli.post(
                f"/api/v4/evolution/mutations/{mutation['mutation_id']}/enable_waiver",
                json={}, headers=_headers(token))
            assert waived.status_code == 200
            assert (await waived.get_json())['final_status'] == 'active'
    _run(go())


def test_mutation_gate_and_recompute_over_http(client):
    cli, token, _store = client
    async def go():
        await cli.post('/api/v4/evolution/goals', json=_goal_payload(
            goal_id='goal-m', criteria=[{'criterion_id': 'c',
                                         'kind': 'explicit_human',
                                         'params': {}}]), headers=_headers(token))
        plan = await cli.post('/api/v4/evolution/goals/goal-m/plans',
                              json={}, headers=_headers(token))
        plan_id = (await plan.get_json())['plan']['plan_id']
        await cli.post(f'/api/v4/evolution/plans/{plan_id}/confirm', json={},
                       headers=_headers(token))
        launch = await cli.post(f'/api/v4/evolution/plans/{plan_id}/launch',
                                json={}, headers=_headers(token))
        episode = (await launch.get_json())['episode']
        await cli.post(f"/api/v4/evolution/episodes/{episode['episode_id']}/verdict",
                       json={'outcome': 'failure'}, headers=_headers(token))

        no_evidence = await cli.post('/api/v4/evolution/mutations', json={
            'kind': 'routing_change', 'payload': {'model': 'x'},
            'evidence_episode_ids': []}, headers=_headers(token))
        assert no_evidence.status_code == 409
        proposed = await cli.post('/api/v4/evolution/mutations', json={
            'kind': 'routing_change', 'payload': {'model': 'x'},
            'evidence_episode_ids': [episode['episode_id']]},
            headers=_headers(token))
        assert proposed.status_code == 201
        mutation = (await proposed.get_json())['mutation']
        confirmed = await cli.post(
            f"/api/v4/evolution/mutations/{mutation['mutation_id']}/confirm",
            json={}, headers=_headers(token))
        assert confirmed.status_code == 200
        assert (await confirmed.get_json())['mutation']['status'] == 'confirmed'

        recompute = await cli.post('/api/v4/evolution/recompute', json={},
                                   headers=_headers(token))
        assert recompute.status_code == 200
        body = await recompute.get_json()
        assert body['steps']['priors']['ok'] is True
        assert 'code' in body['steps']['priors']['result']
        gaps = await cli.get('/api/v4/evolution/capability-gaps',
                             headers=_headers(token))
        assert gaps.status_code == 200
    _run(go())


def test_curriculum_flow_over_http(client):
    cli, token, _store = client
    async def go():
        frontier = await cli.get('/api/v4/evolution/curriculum/frontier',
                                 headers=_headers(token))
        assert frontier.status_code == 200
        body = await frontier.get_json()
        assert set(body['frontier']) >= {'capability_gaps',
                                         'pending_hypotheses',
                                         'anti_patterns', 'domain_rates'}
        proposed = await cli.post('/api/v4/evolution/curriculum/propose',
                                  json={}, headers=_headers(token))
        assert proposed.status_code == 201
        assert (await proposed.get_json())['probes'] == []
    _run(go())


def test_hypotheses_flow_over_http(client, monkeypatch):
    cli, token, _store = client
    from lib.evolution import self_model
    monkeypatch.setattr(self_model, '_default_hypothesis_author',
                        lambda request: [{
                            'feature': 'tool_calls', 'operator': 'gt',
                            'threshold': 50, 'direction': 'hurts',
                            'statement': 'long tool storms fail'}])
    async def go():
        await cli.post('/api/v4/evolution/goals', json=_goal_payload(
            goal_id='goal-h'), headers=_headers(token))
        authored = await cli.post('/api/v4/evolution/hypotheses/author',
                                  json={'domain': 'code'},
                                  headers=_headers(token))
        # No episodes in the domain yet: author short-circuits to [].
        assert authored.status_code == 200
        proposed = await cli.post('/api/v4/evolution/hypotheses', json={
            'hypotheses': [{
                'domain': 'code', 'feature': 'tool_calls', 'operator': 'gt',
                'threshold': 50, 'direction': 'hurts',
                'statement': 'long tool storms fail'}]},
            headers=_headers(token))
        assert proposed.status_code == 201
        validated = await cli.post('/api/v4/evolution/hypotheses/validate',
                                   json={}, headers=_headers(token))
        assert validated.status_code == 200
        listing = await cli.get('/api/v4/evolution/hypotheses',
                                headers=_headers(token))
        assert listing.status_code == 200
        assert len((await listing.get_json())['hypotheses']) == 1
    _run(go())


def test_skill_forge_sandbox_propose_flow_over_http(client, monkeypatch):
    cli, token, _store = client
    from lib.evolution import skill_forge
    monkeypatch.setattr(skill_forge, 'default_author', lambda request: {
        'code': 'def add(a, b):\n    return a + b\n',
        'tests': ('from skill_impl import add\n\n\n'
                  'def test_add():\n    assert add(1, 2) == 3\n')})
    async def go():
        forged = await cli.post('/api/v4/evolution/skills/forge', json={
            'name': 'tiny-adder', 'description': 'adds two numbers'},
            headers=_headers(token))
        assert forged.status_code == 201
        skill = (await forged.get_json())['skill']
        assert skill['status'] == 'draft'

        sandboxed = await cli.post(
            f"/api/v4/evolution/skills/{skill['skill_id']}/sandbox",
            json={}, headers=_headers(token))
        assert sandboxed.status_code == 200
        assert (await sandboxed.get_json())['skill']['status'] == 'sandboxed'

        blocked = await cli.post(
            f"/api/v4/evolution/skills/{skill['skill_id']}/install",
            json={}, headers=_headers(token))
        assert blocked.status_code == 409

        listing = await cli.get('/api/v4/evolution/skills',
                                headers=_headers(token))
        assert len((await listing.get_json())['skills']) == 1
    _run(go())


def test_scope_denied_without_tasks_scope(client):
    from lib.api_keys import create_key
    cli, _token, _store = client
    _row, weak = create_key(owner_user_id=OWNER, name='weak',
                            scopes=['providers'])
    async def go():
        response = await cli.get('/api/v4/evolution/goals',
                                 headers=_headers(weak))
        assert response.status_code == 403
        body = await response.get_json()
        assert body['code'] == 'permission_denied'
        assert 'tasks' in body['detail']
    _run(go())


def test_objective_only_run_api_and_idempotency(client):
    cli, token, _store = client
    async def go():
        headers = {**_headers(token), 'Idempotency-Key': 'headless-objective'}
        response = await cli.post('/api/v4/evolution/runs',
                                  json={'objective': 'Explain binary search.'}, headers=headers)
        assert response.status_code == 201
        run = (await response.get_json())['run']
        assert run['objective'] == 'Explain binary search.'
        assert run['task_id']
        again = await cli.post('/api/v4/evolution/runs',
                               json={'objective': 'Explain binary search.'}, headers=headers)
        assert (await again.get_json())['run']['run_id'] == run['run_id']
        different = await cli.post('/api/v4/evolution/runs',
                                   json={'objective': 'Another objective.'}, headers=headers)
        assert different.status_code == 409
        snapshot = await cli.get(f"/api/v4/evolution/runs/{run['run_id']}", headers=headers)
        assert snapshot.status_code == 200
        listing = await cli.get('/api/v4/evolution/runs', headers=headers)
        listing_body = await listing.get_json()
        assert any(row['run_id'] == run['run_id'] for row in listing_body['runs'])
        floor = listing_body['floor']
        assert any(item['run_id'] == run['run_id'] and item['kind'] == 'evolution'
                   for item in floor['items'])
        proposals = listing_body['proposals']
        assert isinstance(proposals['items'], list)
        assert proposals['degraded'] is False
        invalid = await cli.post('/api/v4/evolution/runs',
                                 json={'objective': 'Explain', 'model': 'manual'}, headers=headers)
        assert invalid.status_code == 400
        cancelled = await cli.post(f"/api/v4/evolution/runs/{run['run_id']}/cancel",
                                   json={}, headers=headers)
        assert (await cancelled.get_json())['run']['status'] == 'cancelled'
    _run(go())


def test_material_shelf_api_survives_reload_and_separates_goals(client):
    cli,token,_=client
    async def go():
        ids=[]
        for objective in ('research papers','repair export'):
            response=await cli.post('/api/v4/evolution/runs',json={'objective':objective},headers=_headers(token))
            assert response.status_code==201
            ids.append((await response.get_json())['run']['run_id'])
        base=f'/api/v4/evolution/runs/{ids[0]}'
        response=await cli.post(base+'/materials',json={'kind':'repository','title':'Example','url':'https://github.com/example/repo'},headers=_headers(token))
        assert response.status_code==200
        item=(await response.get_json())['material']
        detail=await cli.get(base,headers=_headers(token));assert (await detail.get_json())['workspace']['materials']==[item]
        other=await cli.get(f'/api/v4/evolution/runs/{ids[1]}',headers=_headers(token));assert (await other.get_json())['workspace']['materials']==[]
        denied=await cli.post(base+'/materials',json={'kind':'web','title':'Bad','url':'javascript:alert(1)'},headers=_headers(token));assert denied.status_code==400
        missing=await cli.post('/api/v4/evolution/runs/missing/materials',json={},headers=_headers(token));assert missing.status_code==404
        deleted=await cli.delete(base+'/materials/'+item['material_id'],headers=_headers(token));assert deleted.status_code==200
        detail=await cli.get(base,headers=_headers(token));assert not (await detail.get_json())['workspace']['materials']
    _run(go())


def test_goal_updates_conversation_and_ack_projection(client,monkeypatch):
    cli,token,_=client
    wakes=[]
    monkeypatch.setattr('lib.evolution.coordinator.wake',lambda rid,**kw:(wakes.append((rid,kw)) or {'status':'idle'}))
    async def go():
        response=await cli.post('/api/v4/evolution/runs',json={'objective':'Investigate a result'},headers=_headers(token))
        rid=(await response.get_json())['run']['run_id'];base=f'/api/v4/evolution/runs/{rid}'
        headers={**_headers(token),'Idempotency-Key':'owner-message'}
        for _ in range(2):
            result=await cli.post(base+'/updates',json={'kind':'message','content':'Please explain the latest findings'},headers=headers)
            assert result.status_code==200
            assert (await result.get_json())['update']['seq']==1
        listing=await cli.get(base+'/coordination',headers=headers)
        body=await listing.get_json();assert len(body['updates'])==1 and body['acknowledgements']==[]
        detail=await cli.get(base+'/updates/1',headers=headers);assert (await detail.get_json())['update']['origin']=='user'
        invalid=await cli.post(base+'/updates',json={'kind':'assignment','content':'spoof'},headers=headers)
        assert invalid.status_code==400
        missing=await cli.post(base+'/updates',json={'kind':'message','content':'hello'},headers=_headers(token));assert missing.status_code==400
        bad_cursor=await cli.get(base+'/coordination?after=-1',headers=headers);assert bad_cursor.status_code==400
        assert wakes and all(kw['user_id']==OWNER for _,kw in wakes)
    _run(go())


def test_goal_message_is_committed_even_if_coordinator_wake_fails(client,monkeypatch):
    cli,token,_=client
    def offline(*args,**kwargs):raise RuntimeError('model unavailable')
    monkeypatch.setattr('lib.evolution.coordinator.wake',offline)
    async def go():
        response=await cli.post('/api/v4/evolution/runs',json={'objective':'Retain my messages'},headers=_headers(token))
        rid=(await response.get_json())['run']['run_id'];base=f'/api/v4/evolution/runs/{rid}'
        result=await cli.post(base+'/updates',json={'kind':'message','content':'Do not lose this'},headers={**_headers(token),'Idempotency-Key':'offline'})
        assert result.status_code==200
        assert (await result.get_json())['coordinator']['status']=='pending'
        listing=await cli.get(base+'/coordination',headers=_headers(token))
        assert (await listing.get_json())['updates'][0]['content']=='Do not lose this'
    _run(go())


def test_goal_role_model_endpoint_validates_catalog_and_owner(client,monkeypatch):
    cli,token,_=client
    model={'creator_id':'test','model_id':'review'}
    monkeypatch.setattr('lib.evolution.role_models.model_options',lambda **kw:[{'model':model}])
    async def go():
        response=await cli.post('/api/v4/evolution/runs',json={'objective':'Explain'},headers=_headers(token))
        rid=(await response.get_json())['run']['run_id']
        path=f'/api/v4/evolution/runs/{rid}/role-model'
        response=await cli.post(path,json={'role':'reviewer','model':model},headers=_headers(token))
        assert response.status_code==200
        assert (await response.get_json())['run']['role_models']['reviewer']==model
        response=await cli.post(path,json={'role':'reviewer','model':None},headers=_headers(token))
        assert (await response.get_json())['run']['role_models']=={}
        response=await cli.post(path,json={'role':'unknown','model':None},headers=_headers(token))
        assert response.status_code==400
        response=await cli.post(path,json={'role':'reviewer','model':{'creator_id':'missing','model_id':'x'}},headers=_headers(token))
        assert response.status_code==409
        from lib.api_keys import create_key
        _,foreign=create_key(owner_user_id=OWNER+1,name='other-owner',scopes=['tasks'])
        assert (await cli.post(path,json={'role':'reviewer','model':None},headers=_headers(foreign))).status_code==409
    _run(go())


def test_run_execution_policy_contract_and_terminal_snapshot_are_owner_scoped(client, monkeypatch):
    from lib.api_keys import create_key
    from lib.evolution import runs
    cli, token, _ = client
    _, foreign = create_key(owner_user_id=OWNER + 1, name='foreign', scopes=['tasks'])

    async def go():
        headers = {**_headers(token), 'Idempotency-Key': 'execution-policy'}
        response = await cli.post('/api/v4/evolution/runs', json={
            'objective': 'Build a verified deliverable',
            'budget': {'max_tokens': None, 'max_tool_calls': 0}, 'max_attempts': None},
            headers=headers)
        assert response.status_code == 201
        run = (await response.get_json())['run']
        assert run['budget'] == {'max_tokens': 0, 'max_tool_calls': 0, 'max_wall_clock_seconds': 0}
        assert run['max_attempts'] == 0
        path = f"/api/v4/evolution/runs/{run['run_id']}"
        terminal = {'id': run['task_id'], '_userId': OWNER, 'status': 'error',
                    'error': 'execution_token_budget_exhausted',
                    'usage': {'prompt_tokens': 211730}}
        monkeypatch.setattr(runs, 'default_task_loader', lambda tid, user_id:
                            terminal if tid == terminal['id'] and user_id == OWNER else None)
        snapshot = await cli.get(path, headers=headers)
        body = await snapshot.get_json()
        assert body['run']['status'] == 'failed'
        assert body['run']['snapshot_pending_reconciliation'] is True
        assert body['run']['actuals']['tokens'] == 211730
        assert body['workspace']['budget']['max_tokens'] == 0

        listing = await cli.get('/api/v4/evolution/runs', headers=headers)
        panel = await listing.get_json()
        summary = next(row for row in panel['runs'] if row['run_id'] == run['run_id'])
        assert summary['status'] == 'failed'
        assert all(item['run_id'] != run['run_id'] for item in panel['floor']['items'])
        assert runs.get_run(run['run_id'], user_id=OWNER)['status'] == 'executing'
        assert runs.list_runs(user_id=OWNER)[0]['status'] == 'executing'
        assert runs._read(run['run_id'], OWNER)[0]['status'] == 'executing'
        assert (await cli.get(path, headers=_headers(foreign))).status_code == 404
        conflict = await cli.post('/api/v4/evolution/runs', json={
            'objective': run['objective'], 'budget': {'max_tokens': 100}}, headers=headers)
        assert conflict.status_code == 409
        invalid = await cli.post('/api/v4/evolution/runs', json={
            'objective': 'Task', 'budget': {'max_tokens': -1}}, headers=_headers(token))
        assert invalid.status_code == 400
    _run(go())


def test_terminal_outcome_pages_preserve_full_source_without_spending_or_writing(client, monkeypatch):
    http, token, store = client
    headers = _headers(token)
    from lib.evolution import runs
    from lib.evolution.stage_outcomes import capture
    created = _run(http.post('/api/v4/evolution/runs', headers=headers,
                            json={'objective': 'Inspect a committed long source.'}))
    run = _run(created.get_json())['run']
    task = runs.default_task_loader(run['task_id'], user_id=OWNER)
    content = '原始实验🧪' * 1300
    task['content'] = content
    tool_content = '工具实测🧪' * 1400
    task['messages'] = [{'role': 'tool', 'tool_call_id': 'original-probe',
                         'content': tool_content}]
    capture(run, task, user_id=OWNER,
            review_prompt_factory=lambda text: runs._review_prompt({**run, 'result': text}, task))
    writes = store.writes
    path = f"/api/v4/evolution/runs/{run['run_id']}/outcomes/{task['id']}"
    def unexpected(*args, **kwargs):
        pytest.fail('A source GET cannot dispatch a task')
    monkeypatch.setattr('lib.tasks_pkg.spawn.spawn_task', unexpected)
    first = _run(http.get(path, headers=headers))
    assert first.status_code == 200
    source = _run(first.get_json())['outcome']
    assert source['content'] == content[:6000] and source['next_offset'] == 6000
    assert source['terminal_verdict'] == 'completed'
    following = _run(http.get(path + '?offset=6000', headers=headers))
    assert _run(following.get_json())['outcome']['content'] == content[6000:]
    assert source['source_kind'] == 'deliverable' and source['tool_evidence_available'] is True
    tool_page = _run(http.get(path + '?source_kind=tool_evidence', headers=headers))
    assert tool_page.status_code == 200
    tools = _run(tool_page.get_json())['outcome']
    assert tools['source_kind'] == 'tool_evidence'
    assert tools['tool_evidence_scope'] == 'terminal_retained_tool_messages'
    assert tools['source_sha256'] != source['source_sha256'] and tools['next_offset'] == 6000
    tail = _run(http.get(path + '?source_kind=tool_evidence&offset=6000', headers=headers))
    import json
    evidence = json.loads(tools['content'] + _run(tail.get_json())['outcome']['content'])
    assert evidence['content'] == tool_content and evidence['tool_call_id'] == 'original-probe'
    assert tools['actuals'] == source['actuals'] and tools['actuals_complete'] == source['actuals_complete']
    assert _run(http.get(path + '?source_kind=unknown', headers=headers)).status_code == 400
    assert store.writes == writes
    assert _run(http.get(path + '?offset=-1', headers=headers)).status_code == 400
    assert _run(http.get(path + '?offset=invalid', headers=headers)).status_code == 400
    from lib.api_keys import create_key
    _, other = create_key(owner_user_id=OWNER + 1, name='other-outcome-owner', scopes=['tasks'])
    denied = _run(http.get(path, headers=_headers(other)))
    absent = _run(http.get('/api/v4/evolution/runs/absent-run/outcomes/absent-task', headers=_headers(other)))
    assert denied.status_code == absent.status_code == 409
    assert _run(denied.get_json())['detail'] == _run(absent.get_json())['detail']


def test_enable_resumes_owned_admitted_intent_outside_list_without_new_regression(client,monkeypatch):
    http,token,store=client
    from lib import evolution
    from lib.evolution import records
    from lib.evolution.comparison import paired_context_fingerprint
    mutation={'schema_version':1,'mutation_id':'old-admitted-policy','kind':'budget_policy',
        'status':'confirmed','version':1,'parent_version':0,'payload':{'policy':'{"min_lift":0.4}'},
        'evidence_episode_ids':['historical-settlement'],'requires_confirmation':True,
        'created_at':1,'updated_at':2}
    mutation['activation_intent']={'started_at':2,'context_fingerprint':paired_context_fingerprint(mutation)}
    records.put_record(records.NAMESPACE_MUTATIONS,records.entity_key(mutation['mutation_id'],OWNER,kind='mutation'),mutation,kind='fixture')
    for index in range(130):
        row={k:v for k,v in mutation.items() if k!='activation_intent'}
        row.update(mutation_id=f'newer-rejected-{index}',status='rejected',version=index+2,updated_at=3)
        records.put_record(records.NAMESPACE_MUTATIONS,records.entity_key(row['mutation_id'],OWNER,kind='mutation'),row,kind='fixture')
    assert mutation['mutation_id'] not in {row['mutation_id'] for row in evolution.list_mutations(user_id=OWNER)}
    monkeypatch.setattr(evolution,'run_regression',lambda *a,**kw:pytest.fail('Resume must not readmit or launch a probe'))
    response=_run(http.post('/api/v4/evolution/mutations/old-admitted-policy/enable',json={},headers=_headers(token)))
    assert response.status_code==200
    body=_run(response.get_json())
    assert body['final_status']=='active' and 'activation_intent' not in body['mutation']
    assert 'receipt' not in body
    assert evolution.get_thresholds(user_id=OWNER)['min_lift']==0.4
    assert evolution.get_mutation(mutation['mutation_id'],user_id=OWNER)['status']=='active'
    writes=store.writes
    repeated=_run(http.post('/api/v4/evolution/mutations/old-admitted-policy/enable',json={},headers=_headers(token)))
    assert repeated.status_code==200 and _run(repeated.get_json())['final_status']=='active'
    assert store.writes==writes
    with pytest.raises(evolution.EvolutionStateError):evolution.get_mutation(mutation['mutation_id'],user_id=OWNER+1)
