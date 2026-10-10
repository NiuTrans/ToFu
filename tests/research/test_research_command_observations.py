"""Real command envelopes carry measurements without text repair or success guessing."""
import hashlib
import json
import shutil
import subprocess
import pytest
from lib.project_mod.run_command import _format_run_output
from lib.research.action import _tool_receipts,apply_action_result
from lib.research.decisions import manuscript_audit
from lib.research.workspace import empty_workspace
from tests.support.research_observations import observation_payload

pytestmark=pytest.mark.unit
PROTOCOL={'baseline':'reference','dataset':'held-out','primary_metric':'accuracy','random_seeds':[1,2,3]}


def receipt(stdout,*,code=0,stderr='',command='experiment',tool_name='run_command',**flags):
    raw=_format_run_output(command,stdout,stderr,code,**flags)
    row={'toolName':tool_name,'status':'done','toolCallId':'call',
         'toolArgs':json.dumps({'command':command}),'toolContent':raw}
    return _tool_receipts([row])[0],raw


@pytest.mark.parametrize('tool_name', ['run_command', 'code_exec'])
def test_actual_subprocess_stdout_enters_scientific_consistency_audit(tool_name):
    shell=shutil.which('sh')
    if not shell:pytest.skip('POSIX command producer requires a shell')
    payload=observation_payload(PROTOCOL,'metrics','refuted')
    process=subprocess.run([shell,'-c','cat'],input=json.dumps(payload),capture_output=True,text=True,check=True)
    observed,raw=receipt(process.stdout,code=process.returncode,stderr=process.stderr,tool_name=tool_name)
    assert json.loads(observed['observation_excerpt'])==payload
    assert observed['result_digest']==hashlib.sha256(raw.encode()).hexdigest()
    run={'id':'metrics','execution_status':'completed','validity':'current','artifact_ref':'metrics',
         'scientific_outcome':'refuted','tool_receipts':[observed]}
    assert manuscript_audit([run],[{'status':'supported','validity':'current'}],protocol=PROTOCOL)['passed']
    result=apply_action_result(action='experiment',workspace=empty_workspace('Scope'),task_id='task',bindings=[],
        payload={'run':{'id':'metrics','scientific_outcome':'supported'}},receipts=[observed])
    assert result['runs'][0]['scientific_outcome']=='refuted'


@pytest.mark.parametrize('change',['nonzero','timeout','aborted','interrupted'])
@pytest.mark.parametrize('tool_name', ['run_command', 'code_exec'])
def test_failure_footer_wins_over_scientific_success_payload(change, tool_name):
    payload=json.dumps(observation_payload(PROTOCOL,'metrics'))
    flags={'timed_out':True} if change=='timeout' else {'aborted':True} if change=='aborted' else {'interrupted_by':'user'} if change=='interrupted' else {}
    observed,_=receipt(payload,code=1 if change=='nonzero' else 0,tool_name=tool_name,**flags)
    assert observed['status']=='error'
    run={'id':'metrics','execution_status':'completed','validity':'current','artifact_ref':'metrics','tool_receipts':[observed]}
    assert not manuscript_audit([run],[],protocol=PROTOCOL)['passed']


def test_multiline_command_and_stderr_logs_do_not_pollute_json_measurements():
    payload=observation_payload(PROTOCOL,'metrics')
    observed,_=receipt(json.dumps(payload,indent=2),command='experiment \\\n  --seed 1',stderr='Warning: diagnostic-only output')
    assert json.loads(observed['observation_excerpt'])==payload


@pytest.mark.parametrize('text',['log before\n{}','{}\nlog after','{}\n{}','{"scientific_observations":'])
def test_ambiguous_or_incomplete_stdout_is_never_repaired(text):
    observed,_=receipt(text)
    assert 'observation_excerpt' not in observed


def test_background_handoff_is_pending_not_completed_evidence():
    observed=_tool_receipts([{'toolName':'run_command','status':'done',
        'toolContent':'$ experiment\n[Command moved to background: bg_example. Still running]'}])[0]
    assert observed['status']=='pending'
    assert not observed['artifact_ref']


def test_large_wrapped_json_retains_small_scientific_payload_and_full_identity():
    payload=observation_payload(PROTOCOL,'metrics')
    stdout=json.dumps(dict(diagnostics='x'*20000,**payload))
    observed,raw=receipt(stdout)
    assert len(observed['result_excerpt'])==4000
    assert json.loads(observed['observation_excerpt'])==payload
    assert observed['result_digest']==hashlib.sha256(raw.encode()).hexdigest()


def test_command_header_mismatch_is_not_attested_measurement():
    raw=_format_run_output('other command',json.dumps(observation_payload(PROTOCOL,'metrics')),'',0)
    observed=_tool_receipts([{'toolName':'run_command','status':'done','toolArgs':json.dumps({'command':'expected command'}),'toolContent':raw}])[0]
    assert observed['status']=='error' and 'observation_excerpt' not in observed


def test_stderr_json_cannot_replace_a_stdout_result_object():
    observed,_=receipt('{}',stderr=json.dumps(observation_payload(PROTOCOL,'metrics')))
    assert 'observation_excerpt' not in observed
