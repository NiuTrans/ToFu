"""Real HTTP Goal/native source whose caller kills after a committed boundary.

Entry: python -m tests.support.native_goal_crash_probe <configuration.json>.
Uses the installed application fixture's loopback provider and ordinary Flow,
ROOT, billing, checkpoint and file owners. Only boundary waiting is injected;
no checkpoint state, request, invocation identity or accounting is fabricated.
"""
import asyncio
import json
from pathlib import Path
import sys
from types import SimpleNamespace


def definition(count):
    workers = [dict(id='worker-'+str(i), type='role', role='worker', params={
        'objective':'Append one verified result', 'model_override':'alpha', 'tools':['write_file']})
        for i in range(count)]
    nodes = [dict(id='start', type='control', kind='start'), *workers,
        dict(id='review', type='role', role='virtual_user', params={'objective':'Verify the result'}),
        dict(id='stop', type='control', kind='stop')]
    edges = [('start', workers[0]['id']), (workers[0]['id'], 'review')]
    if count > 1:
        nodes += [dict(id='fork', type='control', kind='parallel'),
                  dict(id='join', type='control', kind='barrier')]
        edges = [('start', 'fork'), ('join', 'review')]
        edges += [(a,b) for worker in workers for a,b in
                  [('fork', worker['id']), (worker['id'], 'join')]]
    edges.append(('review', 'stop'))
    return dict(schema='tofu.orchestration/v1', name='Genuine interrupted native Goal',
        nodes=nodes, edges=[{'from':a,'to':b} for a,b in edges])


def run(values):
    import pytest
    from lib.storage import service
    from lib.tasks_pkg.native_checkpoint import NativeCheckpointPolicy
    from lib.tasks_pkg.manager.runtime import chat_task_runtime
    from tests.tasks_pkg.test_native_application_installed import (
        test_http_application_issues_native_root_without_task_sentinel as native_case)
    from tests.support.native_checkpoint_authority import checkpoint_authority

    root = Path(values['directory'])
    count = values['actors']
    snapshots, waiting = {}, set()
    with checkpoint_authority(root/'authority', library=values['library'],
            binary=values.get('binary'), source_sha256=values['source_sha256'],
            transport=values.get('transport', 'embedded'), durability=values.get('durability', 'strict')) as storage, pytest.MonkeyPatch.context() as patch:
        patch.setattr(service, 'storage_runtime', lambda: SimpleNamespace(client=lambda **kwargs: storage))
        from lib.project_mod import modifications
        patch.setattr(modifications, 'SESSIONS_DIR', str(root/'file-sessions'))
        pending, complete = NativeCheckpointPolicy.pending, NativeCheckpointPolicy.complete

        async def hold(policy):
            waiting.add(policy.task['id'])
            if len(waiting) == count:
                assert waiting == set(snapshots)
                parent = chat_task_runtime.get(policy.task['_parentTaskId'])
                assert parent['_goalRunId'] and parent['_flow_projection'] == 'autopilot'
                receipt = dict(run_id=parent['_goalRunId'], task_id=parent['id'],
                    conversation_id=parent['convId'], attempt_id=parent['_attemptId'],
                    wallet_id=parent['_billingUserId'], held_micro=parent['_billing_reservation_micro'],
                    pending=snapshots, phase=values['phase'],
                    account=parent['_executionBudget'].continuity_snapshot())
                if hasattr(storage, '_test_checkpoint_writer'):
                    receipt['checkpoint_writer'] = storage._test_checkpoint_writer
                ready = Path(values['ready'])
                staging = ready.with_suffix('.staging')
                staging.write_text(json.dumps(receipt)+'\n')
                staging.replace(ready)
            await asyncio.Future()  # Parent owns SIGKILL; the serving loop remains responsive.

        async def after_pending(policy, request):
            result = await pending(policy, request)
            if values['phase'] == 'file_not_published':
                await hold(policy)
            return result

        async def before_complete(policy, request):
            if values['phase'] == 'file_published':
                await hold(policy)
            return await complete(policy, request)

        def observe(task, operation, state):
            if operation == 'pending':
                assert task['id'] not in snapshots and state['tools_pending']
                snapshots[task['id']] = state

        patch.setattr(NativeCheckpointPolicy, 'pending', after_pending)
        patch.setattr(NativeCheckpointPolicy, 'complete', before_complete)
        try:
            native_case(storage, root, patch, SimpleNamespace(records=[]), scenario='text',
                prebind_failure=False, resume_operation=None, native_flow=True, native_flow_kernel=True,
                actor_checkpoint=True, actor_tool_round=True, actor_observer=observe,
                flow_definition=definition(count))
        finally:
            storage.close()
    raise AssertionError('Source returned instead of reaching its owned crash boundary')


if __name__ == '__main__':
    run(json.loads(Path(sys.argv[1]).read_text()))
