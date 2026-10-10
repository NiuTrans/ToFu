"""Agent read selection retains ordinary command admission and frozen ownership."""
import hashlib
import shlex
import sys
import pytest
from lib.project_mod import run_command, tools
from tests.evolution.test_evolution_runs import runtime as runtime, OWNER
from tests.evolution.test_evolution_scientific_observations import protocol
from tests.support.research_observations import synthetic_frozen_scope

pytestmark = pytest.mark.unit


def owned(runtime, tmp_path):
    data = b'original';(tmp_path/'data').write_bytes(data)
    frozen = protocol();frozen['measurement_scope'] = synthetic_frozen_scope(frozen)
    frozen['measurement_scope']['candidate_digest'] = hashlib.sha256(data).hexdigest()
    run = runtime[0].submit_run({'objective':'Inspect actual frozen inputs', 'project_path':str(tmp_path),
        'scientific_protocol':frozen,'max_attempts':1},user_id=OWNER)
    return run,runtime[2][-1]


def test_foreign_selection_never_allocates_command(tmp_path):
    command = shlex.join([sys.executable,'-S','-c',"open('marker','w').write('bad')"])
    result = run_command.tool_run_command(str(tmp_path),command,task=None,frozen_input_paths={'candidate_digest':'data'})
    assert 'refused before command allocation' in result and not (tmp_path/'marker').exists()


def test_frozen_digest_drift_refuses_without_command_effect(runtime, tmp_path):
    _,task = owned(runtime,tmp_path);(tmp_path/'data').write_bytes(b'changed')
    command = shlex.join([sys.executable,'-S','-c',"open('marker','w').write('bad')"])
    result = run_command.tool_run_command(str(tmp_path),command,task=task,frozen_input_paths={'candidate_digest':'data'})
    assert 'digest mismatch' in result and not (tmp_path/'marker').exists()


def test_selection_still_runs_network_preflight(runtime, tmp_path, monkeypatch):
    _,task = owned(runtime,tmp_path)
    from lib.project_mod import network_command
    original = network_command.prepare_network_command;seen = []
    def check(*args, **kwargs):
        seen.append(args[0]);return original(*args,**kwargs)
    monkeypatch.setattr(network_command,'prepare_network_command',check)
    result = run_command.tool_run_command(str(tmp_path),'python -c "print(1)"',task=task,frozen_input_paths={'candidate_digest':'data'})
    assert seen == ['python -c "print(1)"']
    assert 'file_read_observations' in result



@pytest.mark.parametrize('selection', [None, 3])
def test_invalid_selection_cannot_take_standalone_read_fastpath(tmp_path, selection):
    result = tools.execute_standalone_command('run_command',{'command':'ls','frozen_input_paths':selection},working_dir=str(tmp_path))
    assert result.startswith('Error: frozen_input_paths')


def test_selection_forwards_after_standalone_fastpath_bypass(tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr(tools,'tool_run_command',lambda *args,**kwargs:seen.append(kwargs) or 'admitted boundary')
    selection = {'candidate_digest':'data'}
    result = tools.execute_standalone_command('run_command',{'command':'ls','frozen_input_paths':selection},working_dir=str(tmp_path))
    assert result == 'admitted boundary' and seen[0]['frozen_input_paths'] == selection


def test_detachable_command_carrier_keeps_owned_selection(runtime, tmp_path):
    from lib.tasks_pkg.handlers._background_command import DetachableCommandTask
    _,task = owned(runtime,tmp_path)
    carrier = DetachableCommandTask(task)
    command = shlex.join([sys.executable,'-S','-c',"open('data','rb').read();print(1)"])
    result = tools.execute_standalone_command('run_command',{'command':command,'frozen_input_paths':{'candidate_digest':'data'}},
        working_dir=str(tmp_path),task=carrier)
    assert 'file_read_observations' in result and '[exit code: 0]' in result
