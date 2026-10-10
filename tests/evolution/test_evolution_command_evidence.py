"""Current executor observations gate reproduction independently of model prose."""
from copy import deepcopy
import pytest
from tests.evolution.test_evolution_runs import runtime as runtime, finish, review, OWNER
from lib.evolution.tool_evidence import command_execution_observed

pytestmark=pytest.mark.unit


def observation():
    return {'toolRounds':[{'toolName':'run_command','toolCallId':'call','status':'done'}],
            'messages':[{'role':'tool','tool_call_id':'call','content':'$ experiment\nactual command output\n[exit code: 0]'}]}


@pytest.mark.parametrize('change',['missing_round','missing_reply','foreign_reply','empty_id',
    'empty_output','pending','error','read_only','structured_error','text_error',
    'malformed_round','malformed_reply_id'])
def test_unproven_or_failed_invocation_does_not_establish_execution(change):
    task=observation()
    if change=='missing_round':task['toolRounds']=[]
    elif change=='missing_reply':task['messages']=[]
    elif change=='foreign_reply':task['messages'][0]['tool_call_id']='other'
    elif change=='empty_id':task['toolRounds'][0]['toolCallId']=''
    elif change=='empty_output':task['messages'][0]['content']=' '
    elif change=='pending':task['toolRounds'][0]['status']='running'
    elif change=='error':task['toolRounds'][0]['status']='error'
    elif change=='read_only':task['toolRounds'][0]['toolName']='read_files'
    elif change=='structured_error':task['messages'][0]['content']='{"ok":false,"error":"failed"}'
    elif change=='malformed_round':task['toolRounds']=[None]
    elif change=='malformed_reply_id':task['messages'][0]['tool_call_id']=[]
    else:task['messages'][0]['content']='Error: command rejected'
    task['content']='I independently reproduced everything successfully.'
    assert not command_execution_observed(task)


def test_successful_correlated_tool_observation_is_pure():
    task=observation();before=deepcopy(task)
    assert command_execution_observed(task)
    assert task==before


@pytest.mark.parametrize('has_observation',[False,True])
def test_required_execution_is_frozen_archived_and_checked_before_review(runtime,has_observation):
    from lib.evolution import get_episode
    from lib.evolution.stage_outcomes import read_metadata
    runs,_,tasks=runtime
    run=runs.submit_run({'objective':'Independently reproduce the frozen comparison.',
        'require_command_execution':True,'max_attempts':1},user_id=OWNER,idempotency_key='command-gate')
    executor=tasks[0]
    if has_observation:executor.update(observation())
    result=finish(runtime,executor,'All results reproduced successfully; this claim alone is not evidence.')
    archived=read_metadata(run['run_id'],executor['id'],user_id=OWNER)
    assert archived['command_execution_observed'] is has_observation
    if has_observation:
        assert result['status']=='reviewing' and len(tasks)==2
        final=finish(runtime,tasks[1],review())
        assert final['status']=='completed'
    else:
        assert result['status']=='failed' and len(tasks)==1
        assert result['error']=='required_command_execution_unobserved'
        assert get_episode(run['episode_id'],user_id=OWNER)['status']=='aborted'
    with pytest.raises(runs.EvolutionStateError):
        runs.submit_run({'objective':run['objective'],'max_attempts':1},user_id=OWNER,idempotency_key='command-gate')


def test_research_discovery_cannot_accept_unenforced_execution_policy(runtime):
    runs,_,tasks=runtime
    with pytest.raises(runs.EvolutionContractError):
        runs.submit_run({'objective':'Discover directions','domain':'research',
            'require_command_execution':True},user_id=OWNER)
    assert not tasks


@pytest.mark.parametrize('exit_code',[0,'0'])
def test_producer_completion_survives_compacted_tool_reply(exit_code):
    from lib.tools.meta import build_project_tool_meta
    task=observation()
    meta=build_project_tool_meta('run_command',{'command':'experiment'},task['messages'][0]['content'])
    meta['exitCode']=exit_code
    task['toolRounds'][0]['results']=[meta]
    task['messages']=[]
    assert command_execution_observed(task)


@pytest.mark.parametrize('state',['background','not-run','timeout',1,-1,False,None])
def test_producer_noncompletion_cannot_be_overruled_by_display(state):
    task=observation()
    task['toolRounds'][0]['results']=[{'exitCode':state}]
    assert not command_execution_observed(task)


@pytest.mark.parametrize('flag',['backgrounded','timedOut','interrupted','notRun'])
def test_disqualifying_execution_flags_win_over_zero_exit(flag):
    task=observation()
    task['toolRounds'][0]['results']=[{'exitCode':'0',flag:True}]
    assert not command_execution_observed(task)


@pytest.mark.parametrize('content',[
    '$ command\n[Command moved to background: bg_123. Still running]',
    '$ command\nstdout says [exit code: 0]\n[exit code: 1]',
    '$ command\n[Command timed out]\n[exit code: -1]',
    'success claimed\n[exit code: 0]'])
def test_legacy_reply_needs_executor_terminal_success_trailer(content):
    task=observation();task['messages'][0]['content']=content
    assert not command_execution_observed(task)


def test_actual_command_completion_producer_survives_removed_model_output():
    import shutil
    import subprocess
    from lib.project_mod.run_command import _format_run_output
    from lib.tools.meta import build_project_tool_meta
    shell=shutil.which('sh')
    if not shell:pytest.skip('POSIX command producer requires a shell')
    process=subprocess.run([shell,'-c','printf measured-result'],capture_output=True,text=True,check=True)
    text=_format_run_output('printf measured-result',process.stdout,process.stderr,process.returncode)
    row={'toolName':'run_command','toolCallId':'executed','status':'done',
         'results':[build_project_tool_meta('run_command',{'command':'printf measured-result'},text)]}
    assert command_execution_observed({'toolRounds':[row],'messages':[]})


@pytest.mark.parametrize('digit', ['0', '9'])
def test_corrupt_oversized_exit_trailer_cannot_abort_execution_evidence(digit):
    from lib.background_command_projection import command_result_status
    from lib.evolution.tool_evidence import background_execution_observed
    task = observation()
    text = '$ experiment\n[exit code: ' + digit * 5000 + ']'
    task['messages'][0]['content'] = text
    assert command_result_status(text) == {}
    assert not command_execution_observed(task)
    assert not background_execution_observed([{'content': text}])


@pytest.mark.parametrize('name', ['run_command', 'code_exec'])
@pytest.mark.parametrize('exit_code', ['0','1'])
def test_project_and_standalone_command_identity_share_success_boundary(name, exit_code):
    task = observation()
    task['toolRounds'][0]['toolName'] = name
    task['toolRounds'][0]['results'] = [{'exitCode': exit_code, 'timedOut': False}]
    assert command_execution_observed(task) is (exit_code == '0')
