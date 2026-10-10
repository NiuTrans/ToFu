"""Actual result settlement retains scientific originals beyond budget/compaction."""
import json
import pytest
from lib.evolution import scientific_commands as commands,tool_evidence,stage_outcomes
from lib.evolution.contracts import EvolutionStateError
from lib.tools.result_value import capture_result,CapturedToolResult,ResultPart,ResultChild,TrustedResultText,persist_result
from tests.evolution.test_evolution_runs import runtime as runtime,finish,OWNER
from tests.evolution.test_evolution_scientific_observations import protocol,command
from tests.support.research_observations import observation_payload

pytestmark=pytest.mark.unit


def settle(runtime,monkeypatch,*,composite=False):
    from lib.tasks_pkg.tool_dispatch import _pipeline
    runs,store,tasks=runtime;frozen=protocol()
    run=runs.submit_run({'objective':'Validate preserved command measurements','scientific_protocol':frozen,'max_attempts':1},user_id=OWNER)
    task=tasks[0];payload=observation_payload(frozen,'metrics','supported')
    payload['padding']='SECRET 原始完整观测 🧪\n'*6000
    # The expensive producer is synthetic; unlike the ordinary display formatter,
    # this complete producer result is supplied before any presentation budget.
    raw='$ measure\n'+json.dumps(payload,ensure_ascii=False)+'\n[exit code: 0]'
    monkeypatch.setattr(_pipeline,'append_event',lambda *a,**k:None)
    monkeypatch.setattr(_pipeline,'run_post_hooks',lambda name,args,text,carrier:text.replace('SECRET','REDACTED'))
    row={'toolName':'run_command','toolCallId':'measure','toolArgs':'{}','status':'searching','results':[{'exitCode':0}]}
    task['toolRounds']=[row]
    name='run_command';content=raw
    if composite:
        name='execute_tools';row['toolName']=name
        child=capture_result(raw.replace('SECRET','REDACTED'))
        content=TrustedResultText('Program summary',canonical=CapturedToolResult((ResultPart('text','Program summary'),),
            children=(ResultChild('run_command','measure',canonical=child),)))
    visible=_pipeline._settle_tool_result(task,name,'measure',{},0,row,content,
        idempotent_tools=frozenset(),cache={},tid=task['id'],round_num=1,settled_results={})
    task['messages'].append({'role':'tool','tool_call_id':'measure','content':str(visible)})
    return run,task,raw.replace('SECRET','REDACTED'),visible


@pytest.mark.parametrize('composite',[False,True])
def test_budgeted_and_compacted_scientific_original_reaches_terminal_review(runtime,monkeypatch,composite):
    run,task,raw,visible=settle(runtime,monkeypatch,composite=composite)
    assert 'SECRET' not in str(visible)
    task['messages']=[message for message in task['messages'] if message.get('role')!='tool']
    # Equivalent to a completed context compaction: the model message is gone.
    audit=tool_evidence.scientific_audit(task,run['scientific_protocol'])
    assert audit['observation_count']==1 and tool_evidence.scientific_audit_valid(audit)
    result=finish(runtime,task,'The paired result is available in original evidence.')
    assert result['status']=='reviewing' and result['tool_evidence_source']
    metadata=stage_outcomes.read_metadata(run['run_id'],task['id'],user_id=OWNER)
    assert 'scientific_command_originals' in metadata['tool_evidence']['scope']
    text=[];offset=0
    while True:
        page=stage_outcomes.page(run['run_id'],task['id'],user_id=OWNER,source_kind='tool_evidence',offset=offset)
        text.append(page['content'])
        if page['next_offset'] is None:break
        offset=page['next_offset']
    originals=[json.loads(line) for line in ''.join(text).splitlines()]
    saved=next(row for row in originals if row.get('source')=='acknowledged_scientific_command_original')
    assert saved['content']==raw and 'SECRET' not in saved['content']


def test_captured_original_does_not_double_count_retained_model_message(runtime,monkeypatch):
    run,task,raw,_=settle(runtime,monkeypatch)
    task['messages']=[{'role':'tool','tool_call_id':'measure','content':raw}]
    audit=tool_evidence.scientific_audit(task,run['scientific_protocol'])
    assert audit['observation_count']==1


@pytest.mark.parametrize('mutation',['part','missing','foreign'])
def test_changed_scientific_original_refuses_before_adjudication(runtime,monkeypatch,mutation):
    run,task,raw,_=settle(runtime,monkeypatch)
    store=runtime[1]
    key=next(key for key in store.rows if key[0]==commands.PARTS_NAMESPACE)
    if mutation=='missing':store.rows.pop(key)
    elif mutation=='part':store.rows[key]['content']='changed'
    else:store.rows[key]['binding']['user_id']=OWNER+1
    with pytest.raises(EvolutionStateError):tool_evidence.scientific_audit(task,run['scientific_protocol'])


def test_capture_refusal_preserves_tool_return_but_never_verifies(runtime,monkeypatch):
    put=commands._put_immutable
    def refuse(namespace,*args,**kwargs):
        if namespace==commands.PARTS_NAMESPACE:raise RuntimeError('capture acknowledgement lost')
        return put(namespace,*args,**kwargs)
    monkeypatch.setattr(commands,'_put_immutable',refuse)
    run,task,raw,visible=settle(runtime,monkeypatch)
    assert visible
    audit=tool_evidence.scientific_audit(task,run['scientific_protocol'])
    assert audit['failure_count'] and not tool_evidence.scientific_audit_valid(audit)
    assert finish(runtime,task,'Measurements cannot yet be verified.')['status']=='failed'


def test_spilled_command_child_is_read_through_existing_owner_artifact_port(runtime):
    runs,_,tasks=runtime;frozen=protocol()
    run=runs.submit_run({'objective':'Retain spilled original','scientific_protocol':frozen},user_id=OWNER)
    task=tasks[0];raw=command(observation_payload(frozen,'metrics'))['messages'][0]['content']
    ref=persist_result(capture_result(raw),user_id=OWNER)
    composite=CapturedToolResult((ResultPart('text','Program summary'),),children=(ResultChild('run_command','spilled',artifact_ref=ref),))
    task['_evolutionScientificCommandCapture'](task,'execute_tools','outer',{},composite)
    audit=tool_evidence.scientific_audit(task,frozen)
    assert audit['observation_count']==1 and tool_evidence.scientific_audit_valid(audit)


def test_close_fence_and_duplicate_capture_preserve_one_original(runtime):
    runs,store,tasks=runtime;frozen=protocol()
    run=runs.submit_run({'objective':'Freeze complete source epoch','scientific_protocol':frozen},user_id=OWNER)
    task=tasks[0];raw=command(observation_payload(frozen,'metrics'))['messages'][0]['content']
    capture=task['_evolutionScientificCommandCapture'];value=capture_result(raw)
    capture(task,'run_command','same-call',{},value);versions=dict(store.versions)
    capture(task,'run_command','same-call',{},value)
    assert store.versions==versions
    audit=tool_evidence.scientific_audit(task,frozen)
    assert tool_evidence.scientific_audit_valid(audit) and audit['observation_count']==1
    with pytest.raises(EvolutionStateError,match='already closed'):
        capture(task,'run_command','late-call',{},value)
    assert not tool_evidence.scientific_audit_valid(tool_evidence.scientific_audit(task,frozen))


def test_foreign_spilled_original_refuses_without_manufacturing_measurement(runtime):
    runs,_,tasks=runtime;frozen=protocol()
    runs.submit_run({'objective':'Reject foreign source','scientific_protocol':frozen},user_id=OWNER)
    task=tasks[0];raw=command(observation_payload(frozen,'metrics'))['messages'][0]['content']
    payload=observation_payload(frozen,'metrics');payload['foreign_only']='distinct foreign original'
    raw=command(payload)['messages'][0]['content']
    ref=persist_result(capture_result(raw),user_id=OWNER+1)
    value=CapturedToolResult((ResultPart('text','Program summary'),),children=(ResultChild('run_command','foreign',artifact_ref=ref),))
    with pytest.raises(EvolutionStateError):task['_evolutionScientificCommandCapture'](task,'execute_tools','outer',{},value)
    assert not tool_evidence.scientific_audit_valid(tool_evidence.scientific_audit(task,frozen))


def test_non_scientific_stages_have_no_command_capture_port(runtime):
    runs,_,tasks=runtime
    runs.submit_run({'objective':'Ordinary goal execution'},user_id=OWNER)
    assert '_evolutionScientificCommandCapture' not in tasks[0]


@pytest.mark.parametrize('bad,code',[
    ('{"scientific_observations":',0),
    ('log prefix\n{"scientific_outcome":"supported"}',0),
    ('{"scientific_outcome":"refuted"}',1),
    ('scientific output mentions "scientific_observations" but has no packet',0),
])
def test_unreadable_captured_scientific_output_cannot_hide_behind_valid_report(runtime,bad,code):
    runs,_,tasks=runtime;frozen=protocol()
    run=runs.submit_run({'objective':'Audit all scientific outputs','scientific_protocol':frozen,'max_attempts':1},user_id=OWNER)
    task=tasks[0];capture=task['_evolutionScientificCommandCapture']
    good=command(observation_payload(frozen,'metrics'))['messages'][0]['content']
    capture(task,'run_command','good',{},capture_result(good))
    capture(task,'run_command','bad',{},capture_result('$ measure\n'+bad+f'\n[exit code: {code}]',execution_status='ok' if code==0 else 'error'))
    audit=tool_evidence.scientific_audit(task,frozen)
    assert audit['observation_count']==2 and audit['failure_count']==1 and audit['outcomes']['supported']==1
    assert not tool_evidence.scientific_audit_valid(audit)
    result=finish(runtime,task,'A favorable result exists, but another output is unverifiable.')
    assert result['status']=='failed' and result['error']=='scientific_observations_unverified'
    assert result['tool_evidence_source']


def test_legacy_capture_policy_cannot_be_promoted_by_merge(runtime):
    from copy import deepcopy
    _,_,tasks=runtime
    frozen=protocol();raw=command(observation_payload(frozen,'metrics'))
    current=tool_evidence.scientific_audit(raw,frozen)
    legacy=deepcopy(current);legacy.pop('capture_policy_version')
    assert tool_evidence.scientific_audit_valid(current)
    assert not tool_evidence.scientific_audit_valid(legacy)
    for first,second in [(legacy,current),(current,legacy)]:
        assert not tool_evidence.scientific_audit_valid(tool_evidence.merge_scientific_audits(first,second))


from tests.evolution.test_evolution_background_execution import command as _background_command_fixture
background_command = _background_command_fixture


@pytest.mark.parametrize('late_outcome',['supported','refuted'])
def test_favorable_foreground_report_waits_for_owned_background_measurement(runtime,background_command,late_outcome):
    from lib.tasks_pkg import background_commands
    from tests.evolution.test_evolution_background_execution import attach_background_launch
    runs,_,tasks=runtime;frozen=protocol()
    run=runs.submit_run({'objective':'Reconcile every admitted experiment','scientific_protocol':frozen,'max_attempts':1},user_id=OWNER)
    task=tasks[0];attach_background_launch(background_command,task)
    task['_evolutionScientificCommandCapture'](task,'run_command','foreground',{},capture_result(command(observation_payload(frozen,'metrics'))['messages'][0]['content']))
    # An asynchronous launch mentioning the scientific protocol is not a report.
    task['_evolutionScientificCommandCapture'](task,'run_command','launch',{},capture_result('$ emit "scientific_observations"\nBackground command started'))
    waiting=finish(runtime,task,'A favorable foreground result is ready; the other experiment is pending.')
    assert waiting['background_wait'] and waiting['status']=='executing' and len(tasks)==1
    metadata=stage_outcomes.read_metadata(run['run_id'],task['id'],user_id=OWNER)
    assert metadata['scientific_audit']['observation_count']==1 and metadata['scientific_audit']['failure_count']==0
    background_commands.finish_handed_off_command(command_id=background_command[1],result=command(observation_payload(frozen,'metrics',late_outcome))['messages'][0]['content'],error=None)
    result=runs.advance_run(run['run_id'],user_id=OWNER)
    assert result['status']==('reviewing' if late_outcome=='supported' else 'failed')
    assert len(tasks)==(2 if late_outcome=='supported' else 1)
    assert stage_outcomes.read_metadata(run['run_id'],task['id'],user_id=OWNER)['background_scientific_audit']['observation_count']==2


@pytest.mark.parametrize('pressure',[False,True])
def test_large_original_reads_bounded_pages_in_numeric_order(runtime,monkeypatch,pressure):
    from lib.storage.errors import StorageError
    runs,_,tasks=runtime
    runs.submit_run({'objective':'Read large originals efficiently','scientific_protocol':protocol()},user_id=OWNER)
    task=tasks[0];raw='"scientific_observations"\n'+('0123456789🧪原文\n'*160000)
    task['_evolutionScientificCommandCapture'](task,'run_command','large',{},capture_result(raw))
    original=commands.records.list_record_page;requests=[]
    def page(namespace,*args,**kwargs):
        if namespace==commands.PARTS_NAMESPACE:
            requests.append((kwargs['limit'],kwargs.get('after_key','')))
            if pressure and kwargs['limit']>2:
                raise StorageError('database_unavailable','database_resource_exhausted')
        return original(namespace,*args,**kwargs)
    monkeypatch.setattr(commands.records,'list_record_page',page)
    def no_single_part(namespace,*args,**kwargs):
        raise AssertionError('Scientific parts must use bounded pages')
    monkeypatch.setattr(commands.records,'get_record',no_single_part)
    assert list(task['_evolutionScientificCommandRead']())[0]['content']==raw
    assert all(limit<=8 for limit,_ in requests)
    assert len(requests)<50
    if pressure:assert requests[:3]==[(8,''),(4,''),(2,'')]


def test_extra_scientific_part_refuses_source_identity(runtime,monkeypatch):
    run,task,_,_=settle(runtime,monkeypatch)
    store=runtime[1]
    key=next(key for key in store.rows if key[0]==commands.PARTS_NAMESPACE)
    from copy import deepcopy
    extra=deepcopy(store.rows[key]);extra['ordinal']=9999
    store.rows[(key[0],key[1].rsplit('-',1)[0]+'-9999')]=extra
    with pytest.raises(EvolutionStateError,match='identity changed'):
        list(task['_evolutionScientificCommandRead']())


def test_compressed_original_preserves_legacy_raw_retry(runtime,monkeypatch):
    runs,store,tasks=runtime
    runs.submit_run({'objective':'Preserve old acknowledged source frames','scientific_protocol':protocol()},user_id=OWNER)
    task=tasks[0];raw='"scientific_observations" '+('原文🧪'*20000)
    with monkeypatch.context() as scoped:
        scoped.setattr(commands,'_encode_part',lambda text:{'content':text})
        task['_evolutionScientificCommandCapture'](task,'run_command','legacy',{},capture_result(raw))
    versions=dict(store.versions)
    task['_evolutionScientificCommandCapture'](task,'run_command','legacy',{},capture_result(raw))
    assert store.versions==versions
    assert list(task['_evolutionScientificCommandRead']())[0]['content']==raw


@pytest.mark.parametrize('damage',['base64','truncated','trailing','bomb','utf8'])
def test_compressed_part_refuses_invalid_or_unbounded_payload(runtime,monkeypatch,damage):
    import base64
    import zlib
    _,task,_,_=settle(runtime,monkeypatch)
    store=runtime[1]
    key=next(key for key in store.rows if key[0]==commands.PARTS_NAMESPACE)
    part=store.rows[key];assert part['encoding']=='zlib-base64'
    packed=base64.b64decode(part['content'])
    if damage=='base64':part['content']='!invalid!'
    else:
        if damage=='truncated':packed=packed[:-1]
        elif damage=='trailing':packed+=b'trailing'
        elif damage=='bomb':packed=zlib.compress(b'x'*(commands.PART_CHARS*4+2))
        else:packed=zlib.compress(b'\xff')
        part['content']=base64.b64encode(packed).decode()
    with pytest.raises(EvolutionStateError,match='compressed part'):
        list(task['_evolutionScientificCommandRead']())


def test_incompressible_original_keeps_raw_frame():
    import random
    import string
    text=''.join(random.Random(73).choices(string.ascii_letters+string.digits,k=commands.PART_CHARS))
    encoded=commands._encode_part(text)
    assert 'encoding' not in encoded and encoded['content']==text


@pytest.mark.parametrize('order', [(0,1,2),(0,2,1),(1,0,2),(1,2,0),(2,0,1),(2,1,0)])
def test_mixed_audit_invalidity_survives_repeated_merge_order_and_grouping(order):
    from copy import deepcopy
    frozen = protocol()
    current = tool_evidence.scientific_audit(command(observation_payload(frozen, 'metrics')), frozen)
    older = deepcopy(current)
    older['audit_version'] -= 1
    audits = [older, deepcopy(current), deepcopy(current)]
    before = deepcopy(audits)
    first, second, third = [audits[index] for index in order]
    merge = tool_evidence.merge_scientific_audits
    left = merge(merge(first, second), third)
    right = merge(first, merge(second, third))
    assert left == right
    assert left['observation_count'] == 3 and left['failure_count'] == 0
    assert left['outcomes']['supported'] == 3
    assert 'capture_policy_version' not in left
    assert not tool_evidence.scientific_audit_valid(left)
    assert audits == before
    homogeneous = merge(merge(current, current), current)
    assert tool_evidence.scientific_audit_valid(homogeneous)
    assert left['evidence_sum'] == homogeneous['evidence_sum']
