"""Two independent Python process images recover a provably unsubmitted stage.

Each process owns and stops its isolated storage runtime. No live application
is restarted; provider execution is replaced only at the task submission port.
"""
import json
import subprocess
import sys

import pytest

pytestmark = pytest.mark.api

CHILD = r"""
import json, sys, os
from pathlib import Path
from lib.storage import StorageRuntime, StorageSupervisor, get_storage_client
from lib.storage.service import install_runtime_for_test
from lib.storage.supervisor import _tofudb_scope_ids
from lib.evolution import runs
from lib.tasks_pkg import spawn
request = json.loads(sys.stdin.read())
owner = _tofudb_scope_ids()[0]
runtime = StorageRuntime(StorageSupervisor(project_root=Path(request['root']),
    backend=request['backend'], startup_timeout=60), auto_restart=False)
install_runtime_for_test(runtime)
submitted = []
spawn.spawn_task = lambda task: submitted.append(task)
runs.resolve_run_model = lambda requested, **kwargs: requested
try:
    runtime.start()
    if request['phase'] == 'birth':
        save = runs._save
        bound = []
        def crash(value, version, user_id):
            result = save(value, version, user_id)
            if value.get('stage_dispatch', {}).get('state') == request.get('loss_state', 'bound'):
                bound.append(value['run_id'])
                raise SystemExit('controlled loss before submission')
            return result
        runs._save = crash
        try:
            runs.submit_run({'objective':'Retain exact original experiment.',
                'budget':{'max_tokens':1000,'max_wall_clock_seconds':600}}, user_id=owner)
        except SystemExit:
            pass
        assert len(bound) == 1 and not submitted
        run = runs.get_run(bound[0], user_id=owner)
        from lib.evolution.stage_inputs import read_input
        prepared = read_input(run, user_id=owner)
        output = {'run_id':run['run_id'],'task_id':run['task_id'],
            'episode_id':run['episode_id'],'attempt':run['attempt'],
            'pid':os.getpid(),'deadline':prepared['config']['executionLimits']['deadline']}
    else:
        get_storage_client(write=True).command('task_results.recover_running',
            {'interrupted_reason':'server_restart','max_rows':500,'scan_limit':10000},None)
        run = runs.advance_run(request['run_id'], user_id=owner)
        expected = 0 if request.get('loss_state') == 'admitted' else 1
        assert len(submitted) == expected
        task = submitted[0] if submitted else None
        runs.advance_run(run['run_id'], user_id=owner)
        assert len(submitted) == expected
        output = {'run_id':run['run_id'],'task_id':run['task_id'],
            'episode_id':run['episode_id'],'attempt':run['attempt'],
            'pid':os.getpid(),'deadline':task['config']['executionLimits']['deadline'] if task else None,
            'submitted_count':len(submitted),'status':run['status'],
            'dispatch_state':run['stage_dispatch']['state'],'receipts':run['receipts']}
    print(json.dumps(output))
finally:
    runtime.stop()
    install_runtime_for_test(None)
"""


@pytest.mark.parametrize('backend', ['sqlite', 'tofudb'])
@pytest.mark.parametrize('loss_state', ['bound', 'admitted'])
def test_fresh_process_recovers_only_proven_unsubmitted_stage(tmp_path, backend, loss_state):
    def child(**fields):
        process = subprocess.run([sys.executable, '-c', CHILD],
            input=json.dumps({'root':str(tmp_path), 'backend':backend, 'loss_state':loss_state, **fields}),
            text=True, capture_output=True, timeout=100)
        assert process.returncode == 0, process.stderr[-6000:]
        return json.loads(process.stdout)
    born = child(phase='birth')
    recovered = child(phase='recover', run_id=born['run_id'])
    assert recovered['pid'] != born['pid']
    assert recovered['run_id'] == born['run_id']
    if loss_state == 'admitted':
        assert recovered['submitted_count'] == 0
        assert recovered['task_id'] == born['task_id']
        assert recovered['episode_id'] == born['episode_id']
        assert recovered['attempt'] == born['attempt']
        assert not any(row.get('submission_voided') for row in recovered['receipts'])
        return
    assert recovered['task_id'] != born['task_id']
    assert recovered['episode_id'] == born['episode_id']
    assert recovered['attempt'] == born['attempt'] == 1
    assert recovered['deadline'] == born['deadline']
    assert recovered['dispatch_state'] == 'admitted'
    assert len(recovered['receipts']) == 1
    receipt = recovered['receipts'][0]
    assert receipt['task_id'] == born['task_id'] and receipt['submission_voided']
    assert receipt['actuals_complete'] is True
    assert receipt['actuals']['tokens'] == receipt['actuals']['tool_calls'] == 0
