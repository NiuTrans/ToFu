"""Repair tasks receive exact previous evidence identities, not only prose."""
from copy import deepcopy
import json
import pytest
from lib.evolution import runs
from tests.evolution.test_evolution_runs import runtime as runtime,finish,review,OWNER
from tests.evolution.test_evolution_background_execution import command as _background_command_fixture
background_command = _background_command_fixture

pytestmark=pytest.mark.unit


def test_all_existing_sources_are_preserved_without_manufactured_identity():
    source=lambda kind:{'task_id':'prior','source_sha256':'a'*64,'total_chars':123,
        **({'source_kind':kind} if kind!='deliverable' else {}),
        **({'complete':False} if kind=='artifact_evidence' else {})}
    run={'run_id':'run-owned','result_source':source('deliverable'),
        'tool_evidence_source':source('tool_evidence'),'artifact_evidence_source':source('artifact_evidence'),
        'background_evidence_source':source('background_evidence')}
    before=deepcopy(run);prompt=runs._prior_source_prompt(run)
    for key in before:
        if key.endswith('_source'):assert json.dumps({**run[key],'run_id':run['run_id']},ensure_ascii=False) in prompt
    assert run==before
    assert runs._prior_source_prompt({'run_id':'run-empty'})==''


def test_late_background_original_reaches_repair_prompt_and_remains_readable(runtime,background_command):
    from tests.evolution.test_evolution_background_execution import attach_background_launch
    from tests.evolution.test_evolution_source_tools import delivered
    from lib.tasks_pkg import background_commands
    from lib.project_mod.run_command import _format_run_output
    active=runs.submit_run({'objective':'Fix the independently observed experiment failure',
        'require_command_execution':True,'max_attempts':2},user_id=OWNER)
    executor=runtime[2][-1];attach_background_launch(background_command,executor)
    waiting=finish(runtime,executor,'Experiment completion is pending.')
    assert waiting['background_wait']
    background_commands.finish_handed_off_command(command_id=background_command[1],
        result=_format_run_output('experiment','BACKGROUND_FALSIFIER: held-out workload regressed','',0),error=None)
    reviewing=runs.advance_run(active['run_id'],user_id=OWNER)
    assert reviewing['status']=='reviewing'
    sources={key:deepcopy(reviewing[key]) for key in ('tool_evidence_source','background_evidence_source') if reviewing.get(key)}
    assert 'background_evidence_source' in sources
    from lib.evolution.source_tools import execute
    listed=execute(runtime[2][-1],{'action':'list','source_kind':'background_evidence'})
    assert len(listed['sources'])==1
    assert listed['sources'][0]['source_sha256']==sources['background_evidence_source']['source_sha256']
    assert not listed['review_read_progress']['all_sources_delivered']
    repaired=finish(runtime,runtime[2][-1],review('fail'))
    assert repaired['status']=='executing' and repaired['attempt']==2
    repair=runtime[2][-1];prompt=repair['messages'][0]['content']
    listed=execute(repair,{'action':'list','source_kind':'background_evidence'})
    assert len(listed['sources'])==1
    assert listed['sources'][0]['task_id']==executor['id']
    assert listed['sources'][0]['source_sha256']==sources['background_evidence_source']['source_sha256']
    for ref in sources.values():
        assert json.dumps({**ref,'run_id':active['run_id']},ensure_ascii=False) in prompt
        page=delivered(repair,{'action':'read','run_id':active['run_id'],'task_id':ref['task_id'],
            'source_kind':ref['source_kind'],'source_sha256':ref['source_sha256'],'offset':0})
        assert 'BACKGROUND_FALSIFIER' in page['content']
    assert repaired['objective']==active['objective'] and repaired['require_command_execution']
