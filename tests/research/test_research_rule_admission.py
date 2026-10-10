"""Every new experiment freezes numeric policy before allocation or effects."""
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock
import threading
import pytest
from tests.research.test_research_action import _epoch
from tests.research.test_research_draft_protocol import client as client, fresh_db  # noqa: F401

pytestmark=pytest.mark.unit
RULE={'direction':'higher','minimum_effect':0,'uncertainty_multiplier':2}


def ports(monkeypatch,workspace,existing=None):
    import lib.research.action_runtime as runtime
    import lib.research.workspace as workspace_module
    production=SimpleNamespace(cleanup_stale=Mock(),index_get=Mock(return_value=existing),
        new_task_id=Mock(return_value='admission-task'),index_register=Mock())
    owner=SimpleNamespace(spawn=Mock(),mark_running=Mock(),abort=Mock(),finish=Mock())
    create=Mock(side_effect=lambda task_id,**kw:dict(task_id=task_id,**kw['fields']))
    production.claim_task=Mock(side_effect=lambda key,task_id,**kw:(create(task_id,**kw),None))
    monkeypatch.setattr(runtime,'_production',production)
    monkeypatch.setattr(runtime,'_research_action_runtime',owner)
    monkeypatch.setattr(runtime,'create_action_task',create)
    monkeypatch.setattr(runtime,'append_action_event',Mock())
    load=Mock(return_value=workspace)
    monkeypatch.setattr(workspace_module,'load_workspace',load)
    return production,owner,create,load


@pytest.mark.parametrize('protocol',[None,{}, {'decision_rule':None},
    {'decision_rule':dict(RULE,uncertainty_multiplier=0)},
    {'decision_rule':dict(RULE,minimum_effect=float('nan'))}])
def test_api_refuses_missing_or_invalid_rule_before_task_allocation(monkeypatch,protocol):
    from lib.research.action import start_research_action
    production,owner,create,load=ports(monkeypatch,{'revision':3,'protocol':protocol})
    with pytest.raises(ValueError):start_research_action('Study',action='experiment',expected_revision=3,
        lang='zh',confirm_external_writes=True,user_id=7)
    load.assert_called_once_with('Study','zh',user_id=7)
    production.new_task_id.assert_not_called();production.index_register.assert_not_called()
    owner.spawn.assert_not_called();create.assert_not_called()


def test_valid_api_admission_preserves_owner_and_exact_revision(monkeypatch):
    from lib.research.action import start_research_action,run_research_action
    workspace={'revision':3,'protocol':{'decision_rule':deepcopy(RULE),'random_seeds':[1,2,3],'dataset':'held-out','primary_metric':'accuracy','baseline':'reference'}}
    production,owner,create,load=ports(monkeypatch,workspace)
    result=start_research_action('Study',action='experiment',expected_revision=3,
        confirm_external_writes=True,user_id=7)
    assert result['task_id']=='admission-task'
    assert create.call_args.kwargs['user_id']==7
    assert create.call_args.kwargs['fields']['expected_revision']==3
    assert owner.spawn.call_args.args[1] is run_research_action
    assert workspace['protocol']['decision_rule']==RULE


def test_stale_revision_is_rejected_before_new_allocation(monkeypatch):
    from lib.research.action import start_research_action
    production,owner,create,load=ports(monkeypatch,{'revision':4,'protocol':{'decision_rule':RULE}})
    with pytest.raises(ValueError,match='research_workspace_stale'):
        start_research_action('Study',action='experiment',expected_revision=3,confirm_external_writes=True,user_id=7)
    production.new_task_id.assert_not_called();owner.spawn.assert_not_called()


def test_acknowledged_dedup_retry_returns_original_task_without_new_effects(monkeypatch):
    from lib.research.action import start_research_action
    production,owner,create,load=ports(monkeypatch,{},existing='original-task')
    result=start_research_action('Study',action='experiment',expected_revision=3,confirm_external_writes=True,user_id=7)
    assert result=={'task_id':'original-task','deduped':True}
    load.assert_not_called();production.new_task_id.assert_not_called();owner.spawn.assert_not_called()


@pytest.mark.parametrize('protocol,revision', [({},3),({'decision_rule':dict(RULE,direction='unknown')},3),
    ({'decision_rule':RULE},4)])
def test_worker_rechecks_admission_before_archival_tool_or_agent_effect(monkeypatch,protocol,revision):
    import lib.research.action as action
    import lib.research.workspace as workspace_module
    import lib.research.history as history
    production,owner,create,load=ports(monkeypatch,{'revision':revision,'protocol':protocol})
    archive=Mock();save=Mock();epoch=Mock();agent=Mock()
    monkeypatch.setattr(history,'compact_observations',archive)
    monkeypatch.setattr(workspace_module,'save_workspace',save)
    monkeypatch.setattr(action,'build_action_tool_epoch',epoch)
    monkeypatch.setattr(action,'_run_agent',agent)
    task={'task_id':'queued','user_id':7,'direction':'Study','lang':'en','action':'experiment',
          'expected_revision':3,'confirm_external_writes':True,'abort_event':threading.Event()}
    action.run_research_action(task)
    assert isinstance(owner.finish.call_args.kwargs['error'],ValueError)
    archive.assert_not_called();save.assert_not_called();epoch.assert_not_called();agent.assert_not_called()


def test_owner_stop_remains_aborted_even_when_legacy_rule_missing(monkeypatch):
    import lib.research.action as action
    production,owner,create,load=ports(monkeypatch,{'revision':3,'protocol':{}})
    stop=threading.Event();stop.set()
    action.run_research_action({'task_id':'stopped','user_id':7,'direction':'Study','lang':'en','action':'experiment',
        'expected_revision':3,'confirm_external_writes':True,'abort_event':stop})
    owner.abort.assert_called_once_with('stopped')
    assert owner.finish.call_args.kwargs['terminal_event_fields']['type']=='aborted'
    assert 'error' not in owner.finish.call_args.kwargs


def test_valid_worker_uses_frozen_rule_and_preserves_stop_before_save(monkeypatch):
    import lib.research.action as action
    import lib.research.workspace as workspace_module
    production,owner,create,load=ports(monkeypatch,{'revision':3,'protocol':{'decision_rule':RULE,'random_seeds':[1,2,3],'dataset':'held-out','primary_metric':'accuracy','baseline':'reference'},'runs':[]})
    stop=threading.Event();save=Mock();seen=[]
    monkeypatch.setattr(workspace_module,'save_workspace',save)
    monkeypatch.setattr(action,'build_action_tool_epoch',lambda **_:(_epoch([]),[],[]))
    def agent(task,workspace,*args):
        seen.append(deepcopy(workspace['protocol']['decision_rule']));stop.set();return {},[]
    monkeypatch.setattr(action,'_run_agent',agent)
    action.run_research_action({'task_id':'valid','user_id':7,'direction':'Study','lang':'en','action':'experiment',
        'expected_revision':3,'confirm_external_writes':True,'abort_event':stop})
    assert seen==[RULE];save.assert_not_called();owner.abort.assert_called_once_with('valid')


@pytest.mark.parametrize('inject_rule',[False,True])
def test_public_task_endpoint_reports_missing_rule_without_spawning(client,monkeypatch,inject_rule):
    from tests.research.test_research_draft_protocol import _post_json
    production,owner,create,load=ports(monkeypatch,{'revision':3,'protocol':{}})
    payload={'kind':'research-action','direction':'Study','action':'experiment',
        'expected_revision':3,'confirm_external_writes':True}
    if inject_rule:payload['decision_rule']=RULE
    status,body=_post_json(client,'/api/v4/tasks/start',payload)
    assert status==400
    assert 'research_decision_rule_required' in str(body)
    production.new_task_id.assert_not_called();owner.spawn.assert_not_called();create.assert_not_called()


def test_legacy_manuscript_action_does_not_require_an_experiment_rule(monkeypatch):
    from lib.research.action import start_research_action
    production,owner,create,load=ports(monkeypatch,{'revision':3})
    result=start_research_action('Study',action='manuscript',expected_revision=3,user_id=7)
    assert result['task_id']=='admission-task'
    owner.spawn.assert_called_once();load.assert_called_once()


def test_numeric_rule_does_not_replace_existing_write_authorization(monkeypatch):
    from lib.research.action import start_research_action
    production,owner,create,load=ports(monkeypatch,{'revision':3,'protocol':{'decision_rule':RULE}})
    with pytest.raises(ValueError,match='confirm_external_writes'):
        start_research_action('Study',action='experiment',expected_revision=3,user_id=7)
    load.assert_not_called();production.new_task_id.assert_not_called();owner.spawn.assert_not_called()


@pytest.mark.parametrize('seeds',[None,[],[1],[1,1],[True,2],['1',2],[1.0,2],list(range(33))])
def test_unverifiable_seed_plan_refuses_before_allocation(monkeypatch,seeds):
    from lib.research.action import start_research_action
    production,owner,create,_=ports(monkeypatch,{'revision':3,'protocol':{
        'decision_rule':deepcopy(RULE),'random_seeds':seeds}})
    with pytest.raises(ValueError,match='research_paired_seeds_required'):
        start_research_action('Study',action='experiment',expected_revision=3,
            confirm_external_writes=True,user_id=7)
    production.new_task_id.assert_not_called();create.assert_not_called();owner.spawn.assert_not_called()


def test_worker_rechecks_seed_plan_before_model_or_archive_effects(monkeypatch):
    import lib.research.action as action
    production,owner,_,_=ports(monkeypatch,{'revision':3,'protocol':{
        'decision_rule':deepcopy(RULE),'random_seeds':[1,1]}})
    agent=Mock();archive=Mock()
    monkeypatch.setattr(action,'_run_agent',agent)
    monkeypatch.setattr('lib.research.history.compact_observations',archive)
    action.run_research_action({'task_id':'queued-seeds','user_id':7,'direction':'Study','lang':'en',
        'action':'experiment','expected_revision':3,'confirm_external_writes':True,
        'abort_event':threading.Event()})
    assert 'research_paired_seeds_required' in str(owner.finish.call_args.kwargs['error'])
    agent.assert_not_called();archive.assert_not_called()


@pytest.mark.parametrize('field',['dataset','primary_metric','baseline'])
@pytest.mark.parametrize('value',[None,'','  ',42,{}])
def test_missing_measurement_context_refuses_before_allocation(monkeypatch,field,value):
    from lib.research.action import start_research_action
    protocol={'decision_rule':deepcopy(RULE),'random_seeds':[1,2],
        'dataset':'held-out','primary_metric':'accuracy','baseline':'reference'}
    protocol[field]=value
    production,owner,create,_=ports(monkeypatch,{'revision':3,'protocol':protocol})
    with pytest.raises(ValueError,match='research_measurement_context_required') as refused:
        start_research_action('Study',action='experiment',expected_revision=3,
            confirm_external_writes=True,user_id=7)
    assert field in str(refused.value)
    production.new_task_id.assert_not_called();create.assert_not_called();owner.spawn.assert_not_called()


def test_worker_rechecks_measurement_context_before_effects(monkeypatch):
    import lib.research.action as action
    _,owner,_,_=ports(monkeypatch,{'revision':3,'protocol':{'decision_rule':deepcopy(RULE),
        'random_seeds':[1,2],'dataset':'held-out','primary_metric':'','baseline':'reference'}})
    agent=Mock();archive=Mock()
    monkeypatch.setattr(action,'_run_agent',agent)
    monkeypatch.setattr('lib.research.history.compact_observations',archive)
    action.run_research_action({'task_id':'queued-context','user_id':7,'direction':'Study','lang':'en',
        'action':'experiment','expected_revision':3,'confirm_external_writes':True,
        'abort_event':threading.Event()})
    assert 'research_measurement_context_required' in str(owner.finish.call_args.kwargs['error'])
    agent.assert_not_called();archive.assert_not_called()


@pytest.mark.parametrize('mutation',['rule','seed','dataset','boolean_rule'])
def test_worker_refuses_mutated_frozen_protocol_before_applying_results(monkeypatch,mutation):
    import lib.research.action as action
    protocol={'decision_rule':deepcopy(RULE),'random_seeds':[1,2,3],
        'dataset':'held-out','primary_metric':'accuracy','baseline':'reference'}
    _,owner,_,_=ports(monkeypatch,{'revision':3,'protocol':protocol,'runs':[]})
    monkeypatch.setattr(action,'build_action_tool_epoch',lambda **_:(_epoch([]),[],[]))
    apply=Mock();save=Mock()
    monkeypatch.setattr(action,'apply_action_result',apply)
    monkeypatch.setattr('lib.research.workspace.save_workspace',save)
    def mutate(task,workspace,*args):
        if mutation=='rule':workspace['protocol']['decision_rule']['minimum_effect']=99
        elif mutation=='boolean_rule':workspace['protocol']['decision_rule']['minimum_effect']=False
        elif mutation=='seed':workspace['protocol']['random_seeds'][0]=999
        else:workspace['protocol']['dataset']='different-dataset'
        return {'summary':'Reported measured result'},[]
    monkeypatch.setattr(action,'_run_agent',mutate)
    action.run_research_action({'task_id':'mutated-protocol','user_id':7,'direction':'Study','lang':'en',
        'action':'experiment','expected_revision':3,'confirm_external_writes':True,'abort_event':threading.Event()})
    assert 'research_protocol_changed_during_execution' in str(owner.finish.call_args.kwargs['error'])
    apply.assert_not_called();save.assert_not_called()


@pytest.mark.parametrize('cancel',[False,True])
def test_epoch_protocol_mutation_refuses_before_agent_dispatch(monkeypatch,cancel):
    import lib.research.action as action
    protocol={'decision_rule':deepcopy(RULE),'random_seeds':[1,2],
        'dataset':'held-out','primary_metric':'accuracy','baseline':'reference'}
    _,owner,_,_=ports(monkeypatch,{'revision':3,'protocol':protocol,'runs':[]})
    agent=Mock();stop=threading.Event()
    def mutated_epoch(**kwargs):
        kwargs['workspace']['protocol']['baseline']='substituted'
        if cancel:stop.set()
        return _epoch([]),[],[]
    monkeypatch.setattr(action,'build_action_tool_epoch',mutated_epoch)
    monkeypatch.setattr(action,'_run_agent',agent)
    action.run_research_action({'task_id':'mutated-epoch','user_id':7,'direction':'Study','lang':'en',
        'action':'experiment','expected_revision':3,'confirm_external_writes':True,'abort_event':stop})
    if cancel:
        owner.abort.assert_called_once_with('mutated-epoch')
        assert 'error' not in owner.finish.call_args.kwargs
    else:assert 'research_protocol_changed_during_execution' in str(owner.finish.call_args.kwargs['error'])
    agent.assert_not_called()


@pytest.mark.parametrize('mutation',['changed','missing','boolean','cancel'])
def test_result_application_cannot_publish_an_unfrozen_protocol(monkeypatch,mutation):
    import lib.research.action as action
    protocol={'decision_rule':deepcopy(RULE),'random_seeds':[1,2,3],
        'dataset':'held-out','primary_metric':'accuracy','baseline':'reference'}
    _,owner,_,_=ports(monkeypatch,{'revision':3,'protocol':protocol,'runs':[]})
    stop=threading.Event();save=Mock()
    monkeypatch.setattr(action,'build_action_tool_epoch',lambda **_:(_epoch([]),[],[]))
    monkeypatch.setattr(action,'_run_agent',lambda *args:({'summary':'Measured result'},[]))
    monkeypatch.setattr('lib.research.workspace.save_workspace',save)
    def apply(**kwargs):
        updated=deepcopy(kwargs['workspace'])
        if mutation=='missing':updated.pop('protocol')
        elif mutation=='boolean':updated['protocol']['decision_rule']['minimum_effect']=False
        else:updated['protocol']['baseline']='different-baseline'
        if mutation=='cancel':stop.set()
        return updated
    monkeypatch.setattr(action,'apply_action_result',apply)
    action.run_research_action({'task_id':'result-protocol','user_id':7,'direction':'Study','lang':'en',
        'action':'experiment','expected_revision':3,'confirm_external_writes':True,'abort_event':stop})
    save.assert_not_called()
    if mutation=='cancel':
        owner.abort.assert_called_once_with('result-protocol')
        assert 'error' not in owner.finish.call_args.kwargs
    else:assert 'research_protocol_changed_before_commit' in str(owner.finish.call_args.kwargs['error'])


def test_unchanged_result_protocol_can_commit_with_original_owner_revision(monkeypatch):
    import lib.research.action as action
    workspace={'revision':3,'protocol':{'decision_rule':deepcopy(RULE),'random_seeds':[1,2,3],
        'dataset':'held-out','primary_metric':'accuracy','baseline':'reference'},'runs':[]}
    _,owner,_,_=ports(monkeypatch,workspace)
    save=Mock(return_value={**deepcopy(workspace),'revision':4})
    monkeypatch.setattr('lib.research.workspace.save_workspace',save)
    monkeypatch.setattr(action,'build_action_tool_epoch',lambda **_:(_epoch([]),[],[]))
    monkeypatch.setattr(action,'_run_agent',lambda *args:({'summary':'Measured result'},[]))
    monkeypatch.setattr(action,'apply_action_result',lambda **kwargs:deepcopy(kwargs['workspace']))
    action.run_research_action({'task_id':'stable-protocol','user_id':7,'direction':'Study','lang':'en',
        'action':'experiment','expected_revision':3,'confirm_external_writes':True,'abort_event':threading.Event()})
    save.assert_called_once()
    assert save.call_args.kwargs['expected_revision']==3 and save.call_args.kwargs['user_id']==7
    assert 'error' not in owner.finish.call_args.kwargs


@pytest.mark.parametrize('phase',['epoch','agent','apply'])
@pytest.mark.parametrize('change',['replace','remove'])
@pytest.mark.parametrize('cancel',[False,True])
def test_experiment_cannot_rebind_observations_to_changed_hypothesis(monkeypatch,phase,change,cancel):
    import lib.research.action as action
    workspace={'revision':3,'hypothesis':'Candidate improves held-out accuracy',
        'protocol':{'decision_rule':deepcopy(RULE),'random_seeds':[1,2,3],
            'dataset':'held-out','primary_metric':'accuracy','baseline':'reference'},'runs':[]}
    _,owner,_,_=ports(monkeypatch,workspace)
    save=Mock();applied=Mock();stop=threading.Event()
    monkeypatch.setattr('lib.research.workspace.save_workspace',save)
    def mutate(target):
        if change=='remove':target.pop('hypothesis')
        else:target['hypothesis']='A different post-measurement claim'
        if cancel:stop.set()
    def epoch(**kwargs):
        if phase=='epoch':mutate(kwargs['workspace'])
        return _epoch([]),[],[]
    def agent(task,current,*args):
        if phase=='agent':mutate(current)
        return {'summary':'Reported measurement'},[]
    def apply(**kwargs):
        applied();updated=deepcopy(kwargs['workspace'])
        if phase=='apply':mutate(updated)
        return updated
    monkeypatch.setattr(action,'build_action_tool_epoch',epoch)
    monkeypatch.setattr(action,'_run_agent',agent)
    monkeypatch.setattr(action,'apply_action_result',apply)
    action.run_research_action({'task_id':'hypothesis-fence','user_id':7,'direction':'Study','lang':'en',
        'action':'experiment','expected_revision':3,'confirm_external_writes':True,'abort_event':stop})
    reason='research_hypothesis_changed_before_commit' if phase=='apply' else 'research_hypothesis_changed_during_execution'
    if cancel:
        owner.abort.assert_called_once_with('hypothesis-fence')
        assert 'error' not in owner.finish.call_args.kwargs
    else:assert reason in str(owner.finish.call_args.kwargs['error'])
    save.assert_not_called()
    if phase!='apply':applied.assert_not_called()
