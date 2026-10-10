"""Frozen input expectations originate in owned run state, never executor hashes."""
import hashlib
import shutil
import sys
import pytest
from lib.evolution import input_binding
from lib.evolution.contracts import EvolutionStateError
from lib.project_mod.file_read_probe import run_observed_process
from lib.project_mod.run_command import _get_cmd_env
from tests.evolution.test_evolution_runs import runtime as runtime, OWNER
from tests.evolution.test_evolution_scientific_observations import protocol
from tests.support.research_observations import synthetic_frozen_scope

pytestmark = pytest.mark.unit


@pytest.fixture
def bound(runtime, tmp_path):
    data = b'owned frozen candidate';(tmp_path/'candidate').write_bytes(data)
    frozen = protocol();frozen['measurement_scope'] = synthetic_frozen_scope(frozen)
    frozen['measurement_scope']['candidate_digest'] = hashlib.sha256(data).hexdigest()
    run = runtime[0].submit_run({'objective':'Validate owned inputs', 'project_path':str(tmp_path),
        'scientific_protocol':frozen,'max_attempts':1},user_id=OWNER)
    return run,runtime[2][-1],frozen


def test_preparation_is_read_only_and_expected_map_is_defensive(runtime, bound):
    run,task,frozen = bound;writes = runtime[1].writes
    plan = input_binding.prepare_input_binding(task,{'candidate_digest':'candidate'})
    assert plan.expected_input_sha256 == {'candidate':frozen['measurement_scope']['candidate_digest']}
    result = plan.expected_input_sha256;result['candidate'] = '0'*64
    plan.check_current(task)
    assert plan.expected_input_sha256['candidate'] != '0'*64
    assert runtime[1].writes == writes


@pytest.mark.parametrize('selection', [{}, {'budget':'candidate'}, {'candidate_digest':'../outside'}, {'candidate_digest':'candidate','baseline_digest':'candidate'}])
def test_invalid_or_conflicting_bindings_refuse(runtime, bound, selection):
    with pytest.raises((EvolutionStateError,OSError,ValueError)):
        input_binding.prepare_input_binding(bound[1],selection)


@pytest.mark.parametrize('change', ['executor','protocol','project','cancelled'])
def test_binding_revalidates_owned_state_at_admission(runtime, bound, tmp_path, change):
    run,task,_ = bound;plan = input_binding.prepare_input_binding(task,{'candidate_digest':'candidate'})
    current,version = runtime[0]._read(run['run_id'],OWNER)
    if change == 'executor': current['task_id'] = 'replacement'
    elif change == 'protocol': current['scientific_protocol']['decision_rule']['minimum_effect'] += 1
    elif change == 'project': current['project_path'] = str(tmp_path/'other');(tmp_path/'other').mkdir();(tmp_path/'other/candidate').write_bytes(b'owned frozen candidate')
    else: task['aborted'] = True
    if change != 'cancelled': runtime[0]._save(current,version,OWNER)
    with pytest.raises((EvolutionStateError,ValueError)):
        plan.check_current(task)


def test_protocol_change_during_file_hash_prevents_real_command(runtime, bound, tmp_path, monkeypatch):
    tracer = shutil.which('strace')
    if not tracer: pytest.skip('Optional Linux strace unavailable')
    run,task,_ = bound;plan = input_binding.prepare_input_binding(task,{'candidate_digest':'candidate'})
    from lib.project_mod import file_read_probe
    original = file_read_probe.os.read;changed = [False]
    def mutate(*args):
        data = original(*args)
        if not changed[0] and data:
            changed[0] = True
            current,version = runtime[0]._read(run['run_id'],OWNER)
            current['scientific_protocol']['decision_rule']['minimum_effect'] += 1
            runtime[0]._save(current,version,OWNER)
        return data
    monkeypatch.setattr(file_read_probe.os,'read',mutate)
    with pytest.raises(EvolutionStateError,match='binding changed'):
        run_observed_process(plan.project_path,list(plan.expected_input_sha256),
            [sys.executable,'-S','-c',"open('marker','w').write('bad')",'@tofu-input:candidate'],
            timeout_seconds=5,tracer=tracer,task=task,child_env=_get_cmd_env(plan.project_path),pin_inputs=True,
            expected_input_sha256=plan.expected_input_sha256,admission_check=lambda:plan.check_current(task))
    assert not (tmp_path/'marker').exists()


@pytest.mark.parametrize('change', ['foreign_owner','reviewer'])
def test_foreign_or_reviewing_carrier_cannot_prepare_input_grants(runtime, bound, change):
    from copy import deepcopy
    task = dict(bound[1]);task['config'] = deepcopy(task['config'])
    if change == 'foreign_owner': task['_userId'] = OWNER + 1
    else: task['config']['evolution']['stage'] = 'reviewing'
    with pytest.raises((EvolutionStateError,ValueError)):
        input_binding.prepare_input_binding(task,{'candidate_digest':'candidate'})


def test_owned_plan_and_admission_allow_expected_real_command(runtime, bound):
    tracer = shutil.which('strace')
    if not tracer: pytest.skip('Optional Linux strace unavailable')
    run,task,_ = bound;plan = input_binding.prepare_input_binding(task,{'candidate_digest':'candidate'})
    report = run_observed_process(plan.project_path,list(plan.expected_input_sha256),
        [sys.executable,'-S','-c',"import sys;assert open(sys.argv[1],'rb').read()==b'owned frozen candidate'",'@tofu-input:candidate'],
        timeout_seconds=5,tracer=tracer,task=task,child_env=_get_cmd_env(plan.project_path),pin_inputs=True,
        expected_input_sha256=plan.expected_input_sha256,admission_check=lambda:plan.check_current(task))
    assert report['returncode'] == 0 and report['inputs'][0]['matches_expected']
