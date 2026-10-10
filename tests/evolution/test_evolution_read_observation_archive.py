"""Read facts survive command compaction without becoming numerical measurements."""
import json
import pytest
from lib.project_mod.file_read_probe import command_evidence_text, FILE_READ_OBSERVATION_KEY
from lib.project_mod.run_command import _format_run_output
from lib.evolution import tool_evidence, stage_outcomes
from tests.evolution.test_evolution_runs import runtime as runtime, finish, OWNER
from tests.evolution.test_evolution_scientific_observations import protocol
from tests.support.research_observations import observation_payload

pytestmark = pytest.mark.unit


def settle(runtime, monkeypatch, task, call, raw, command_display="measure"):
    from lib.tasks_pkg.tool_dispatch import _pipeline
    monkeypatch.setattr(_pipeline,'append_event',lambda *args,**kwargs:None)
    monkeypatch.setattr(_pipeline,'run_post_hooks',lambda name,args,text,carrier:text)
    row = {'toolName':'run_command','toolCallId':call,'toolArgs':json.dumps({'command':command_display}),
        'status':'searching','results':[{'exitCode':0}]}
    task.setdefault('toolRounds',[]).append(row)
    _pipeline._settle_tool_result(task,'run_command',call,{'command':command_display},0,row,raw,
        idempotent_tools=frozenset(),cache={},tid=task['id'],round_num=1,settled_results={})


@pytest.mark.parametrize('with_measurement', [False,True])
def test_compacted_observation_original_is_archived_and_does_not_pollute_scientific_audit(runtime, monkeypatch, with_measurement):
    frozen = protocol();run = runtime[0].submit_run({'objective':'Inspect original execution and reads',
        'scientific_protocol':frozen,'max_attempts':1},user_id=OWNER)
    task = runtime[2][-1]
    stdout = json.dumps(observation_payload(frozen,'metrics','supported')) if with_measurement else 'Setup only; no metric.'
    command = _format_run_output('measure',stdout,'',0)
    observed = command_evidence_text(command,{'format':'tofu.experiment-read-probe/v1',
        'inputs':[{'path':'dataset','read_bytes':12}], 'trust':'read observation, no scientific approval'})
    settle(runtime,monkeypatch,task,'reads',observed)
    task['messages'] = [message for message in task['messages'] if message.get('role') != 'tool']
    audit = tool_evidence.scientific_audit(task,frozen)
    assert audit['observation_count'] == int(with_measurement) and audit['failure_count'] == 0
    if not with_measurement:
        assert not tool_evidence.scientific_audit_valid(audit)
        assert any(FILE_READ_OBSERVATION_KEY in message['content'] for message in task['_evolutionScientificCommandRead']())
        failed = finish(runtime,task,'Read observations only; no numerical measurement was made.')
        assert failed['status'] == 'failed'
        return
    assert tool_evidence.scientific_audit_valid(audit)
    finished = finish(runtime,task,'Original numerical result and read facts are preserved.')
    assert finished['status'] == 'reviewing'
    parts = [];offset = 0
    while True:
        page = stage_outcomes.page(run['run_id'],task['id'],user_id=OWNER,source_kind='tool_evidence',offset=offset)
        parts.append(page['content'])
        if page['next_offset'] is None: break
        offset = page['next_offset']
    rows = [json.loads(line) for line in ''.join(parts).splitlines()]
    assert any(FILE_READ_OBSERVATION_KEY in row.get('content','') for row in rows)


def test_nonmeasurement_read_original_does_not_make_later_valid_measurement_fail(runtime, monkeypatch):
    frozen = protocol();run = runtime[0].submit_run({'objective':'Measure after inspected setup',
        'scientific_protocol':frozen,'max_attempts':1},user_id=OWNER);task = runtime[2][-1]
    settle(runtime,monkeypatch,task,'setup',command_evidence_text(_format_run_output('measure','Setup','',0),{'inputs':[]}))
    settle(runtime,monkeypatch,task,'measurement',_format_run_output('measure',json.dumps(observation_payload(frozen,'metrics','supported')),'',0))
    task['messages'] = [message for message in task['messages'] if message.get('role') != 'tool']
    audit = tool_evidence.scientific_audit(task,frozen)
    assert audit['observation_count'] == 1 and audit['failure_count'] == 0
    assert tool_evidence.scientific_audit_valid(audit)


def test_observation_format_preserves_command_stdout_stderr_and_failure_status():
    from lib.research.action import _observed_tool_payload
    from lib.background_command_projection import command_result_status
    raw = _format_run_output('measure','{"value":12}','original stderr',0)
    enriched = command_evidence_text(raw,{'inputs':[],'command_result':'must not duplicate'})
    payload,state = _observed_tool_payload({'toolName':'run_command','toolArgs':{'command':'measure'}},enriched)
    assert payload == {'value':12} and state == 'done' and 'original stderr' in enriched
    failed = command_evidence_text(_format_run_output('measure','','',-1,aborted=True),{'inputs':[]})
    assert command_result_status(failed)['exitCode'] == -1
    assert command_result_status(failed).get('interrupted')


@pytest.mark.parametrize('flags', [{}, {'timed_out':True}, {'aborted':True}, {'interrupted_by':'operator'}])
def test_observation_preserves_exact_producer_terminal_classification(flags):
    from lib.background_command_projection import command_result_status
    raw = _format_run_output('measure','partial stdout','',-1,**flags)
    assert command_result_status(command_evidence_text(raw,{'inputs':[]})) == command_result_status(raw)


def test_real_negative_experiment_keeps_read_observation_after_compaction(runtime, monkeypatch, tmp_path):
    import runpy
    import shutil
    import sys
    from pathlib import Path
    from lib.research.program import normalize_program_fields
    from lib.project_mod.file_read_probe import run_observed_process
    from lib.project_mod.run_command import _get_cmd_env
    tracer = shutil.which('strace')
    if not tracer: pytest.skip('Optional Linux strace unavailable')
    script = tmp_path/'measure.py'
    shutil.copy2(Path(__file__).parents[1]/'fixtures/evolution/paired_search_measurement.py',script)
    measured = runpy.run_path(str(script))['measure']()['scientific_observations']['comparison']['method']
    frozen = normalize_program_fields({'protocol':{'dataset':'seeded-search-queries','baseline':'binary-control',
        'primary_metric':'comparisons','random_seeds':[11,23,37],
        'decision_rule':{'direction':'lower','minimum_effect':0,'uncertainty_multiplier':2}}})['protocol']
    frozen['measurement_scope'] = {key:measured[key] for key in ('dataset_digest','split_digest','budget_unit','budget')}
    frozen['measurement_scope'].update(candidate_digest=measured['system_digest'],baseline_digest=measured['system_digest'])
    run = runtime[0].submit_run({'objective':'Measure actual paired search counts', 'project_path':str(tmp_path),
        'scientific_protocol':frozen,'scientific_required_outcome':'supported','max_attempts':1},user_id=OWNER)
    task = runtime[2][-1]
    report = run_observed_process(tmp_path,['measure.py'],[sys.executable,'-S',str(script)],
        timeout_seconds=5,tracer=tracer,task=task,child_env=_get_cmd_env(str(tmp_path)))
    assert report['returncode'] == 0 and report['inputs'][0]['read_calls'] > 0
    import shlex
    settle(runtime,monkeypatch,task,'real-read',report['evidence_result'],shlex.join([sys.executable,'-S',str(script)]))
    task['messages'] = [message for message in task['messages'] if message.get('role') != 'tool']
    audit = tool_evidence.scientific_audit(task,frozen)
    assert tool_evidence.scientific_audit_valid(audit) and audit['outcomes']['refuted'] == 1
    finished = finish(runtime,task,'The candidate wins despite its measured negative result.')
    assert finished['status'] == 'failed' and finished['error'] == 'scientific_outcome_requirement_unmet'
    chunk = stage_outcomes.page(run['run_id'],task['id'],user_id=OWNER,source_kind='tool_evidence',offset=0)
    assert FILE_READ_OBSERVATION_KEY in chunk['content'] and 'refuted' in chunk['content']
