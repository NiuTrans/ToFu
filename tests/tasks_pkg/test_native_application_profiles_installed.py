"""Actual native actor Flow and ordinary ROOT settle in the same application."""
import asyncio
import json
import time
import uuid

import pytest

from tests.tasks_pkg.test_native_application_installed import (
    application_storage as application_storage,
    test_http_application_issues_native_root_without_task_sentinel as native_case,
)

pytestmark = pytest.mark.api


@pytest.fixture
def application_storage_owner_id():
    return 2


@pytest.mark.parametrize('production_profile',[False,True])
def test_actor_checkpoint_flow_and_ordinary_root_share_the_native_application(
        application_storage, tmp_path, monkeypatch, caplog, production_profile):
    from lib.billing import get_balance
    from lib.billing.request_flow import settle_task
    from lib.goal_runs.native_recovery import NativeGoalRecoveryConfiguration, recovery_options
    from lib.tasks_pkg.native_application import NativeRootConfiguration
    from lib.turn_lifecycle import get_attempt, get_turn
    from tests.tasks_pkg.test_native_application import profile

    snapshots = []
    def observe(task, operation, state):
        assert operation in {'pending', 'complete'}
        assert state['shared_model_account']['actor_task_id'] == task['id']
        assert state['shared_model_account']['root_task_id'] != task['id']
        snapshots.append((operation, state))

    def configure(*, configuration, checkpoint_factory, **options):
        root = profile()
        root['model_counter'] = dict(configuration['model_counter'])
        flow = json.loads(json.dumps(configuration))
        flow['terminal_persistence']['ack_ms'] = 7000
        if production_profile:
            from lib.agent_core.native_process import Budget
            from lib.tasks_pkg.native_production_profile import compile_native_application_profile
            root=compile_native_application_profile(transport=Budget(1,16<<20,1<<20,1<<20,5),
                launch_policy={'frame_maximum':1<<20})
            flow.update(json.loads(json.dumps(root)))
        return NativeRootConfiguration(
            configuration=root, flow_configuration=flow,
            flow_checkpoint_factory=checkpoint_factory,
            goal_recovery=NativeGoalRecoveryConfiguration(storage_factory=lambda: application_storage),
            **options)

    async def ordinary_root(app, provider, created, owner, checkpoints, ordinary_provider_post):
        host = app.extensions['tofu_native_host']
        assert len(checkpoints) == 2
        assert [operation for operation, _ in snapshots].count('pending') == 2
        assert [operation for operation, _ in snapshots].count('complete') == 2
        assert len({state['task_id'] for _, state in snapshots}) == 2
        files = list(tmp_path.glob('actor-*.txt'))
        assert len(files) == 2 and all(path.read_text() == 'once\n' for path in files)
        assert all(checkpoint.closed for checkpoint in checkpoints)
        assert not host._clients
        provider.server.RequestHandlerClass.do_POST = ordinary_provider_post
        async with app.app_context():
            import math
            assert recovery_options()['pending_actor_bounds']['io_timeout_ms'] == math.ceil(application_storage.request_timeout_seconds*1000)
        previous_requests = len(provider.bodies)
        previous_tasks = len(created)
        wallet = created[-1][0]['_billingUserId']
        balance = get_balance(wallet)
        conv_id = uuid.uuid4().hex
        await asyncio.to_thread(application_storage.command, 'conversation.create', dict(
            conv_id=conv_id, user_id=owner, title='Ordinary ROOT after native Flow',
            created_at=1, updated_at=1, settings={}), uuid.uuid4().hex)
        body = dict(commandId=uuid.uuid4().hex, inputTurn={'content':'ordinary root after flow'}, config={
            'modelRef':{'creator_id':'creator','model_id':'alpha'}, 'maxTokens':64,
            'thinkingEnabled':False, 'memoryEnabled':False, 'projectEnabled':False,
            'searchMode':'off', 'tools':{'programmaticCalling':'off'}})
        response = await app.test_client().post(f'/api/v4/conversations/{conv_id}/turns', json=body)
        payload = await response.get_json()
        assert response.status_code == 200 and payload['ok'], payload
        expires = time.monotonic() + 15
        while True:
            attempt = await asyncio.to_thread(get_attempt, payload['attempt']['attemptId'], user_id=owner)
            if attempt['status'] in {'completed','failed','interrupted'}:
                break
            assert time.monotonic() < expires, attempt
            await asyncio.sleep(.02)
        turn = await asyncio.to_thread(get_turn, conv_id, payload['turn']['turnId'], user_id=owner)
        assert attempt['status'] == turn['status'] == 'completed', turn
        assert turn['projection']['content'] == 'durable fixture answer'
        assert turn['projection']['usage']['total_tokens'] == 5
        assert len(created) == previous_tasks + 1
        task, route = created[-1]
        while host._clients or not route.disposed or task.get('_nativePreparedInvocation') is not None:
            assert time.monotonic() < expires, 'ordinary ROOT owners did not join'
            await asyncio.sleep(.02)
        replay = await asyncio.to_thread(application_storage.query, 'task_results.replay_get',
            {'key':task['id'], 'user_id':owner})
        assert replay['status'] == 'done'
        assert not task.get('flow_mode') and task['_nativeFrozenCostProjection']
        assert len(task['apiRounds']) == 1
        assert task['apiRounds'][0]['taskId'] == task['id']
        assert len(provider.bodies) == previous_requests + 1
        assert len(snapshots) == 4 and len(checkpoints) == 2, 'ordinary ROOT acquired actor recovery'
        assert set(tmp_path.glob('actor-*.txt')) == set(files)
        charge = task['apiRounds'][0]['nativeCredits']['total']
        assert settle_task(task, user_id=wallet, model='alpha', raise_on_error=True)['cost_micro'] == charge
        assert get_balance(wallet) == balance - charge
        assert recovery_options(task) == {}
        return dict(scope='ordinary ROOT following actor-checkpoint Flow in the same application',
            provider_requests=1, total_provider_requests=len(provider.bodies),
            attempt_status=attempt['status'], turn_status=turn['status'], replay_status=replay['status'],
            actor_checkpoints_unchanged=len(checkpoints), durable_actor_frontiers=len(snapshots), native_root=True,
            route_retired=route.disposed, native_clients_joined=not host._clients,
            wallet_charge=charge, wallet_balance_before=balance, wallet_balance_after=get_balance(wallet))

    native_case(application_storage, tmp_path, monkeypatch, caplog,
        scenario='text', prebind_failure=False, resume_operation=None,
        native_flow=True, native_flow_kernel=True, flow_archive=True,
        actor_checkpoint=True, actor_observer=observe, actor_tool_round=True,
        application_configuration_factory=configure, after_completion=ordinary_root)
