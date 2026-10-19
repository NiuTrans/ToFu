"""Complete declared generations, immutable review and failed-capture settlement."""
import base64
import hashlib
import json
import os
from concurrent.futures import ThreadPoolExecutor

import pytest

from tests.evolution.test_evolution_runs import runtime as task_runtime, finish, review, OWNER
from lib.evolution.contracts import EvolutionStateError
from tests.evolution.test_evolution_source_tools import delivered

runtime = task_runtime
pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def no_reindex(monkeypatch):
    monkeypatch.setattr('lib.evolution.maintenance.reindex_maintenance', lambda **kw: [])


def project_run(runtime, tmp_path):
    run = runtime[0].submit_run({'objective':'Independently inspect the complete artifact.',
        'project_path':str(tmp_path),'max_attempts':1}, user_id=OWNER)
    return run, runtime[2][-1]


def artifact_review(runtime, tmp_path, content='Full result 🧪\n' * 2000):
    from lib.evolution.artifact_evidence import declare
    run, executor = project_run(runtime,tmp_path)
    path = tmp_path/'result.json';path.write_text(content)
    snapshot = declare(executor,{'path':'result.json'})
    # Only a head goes into tool evidence, exactly the real-provider failure shape.
    executor['messages']=[{'role':'tool','tool_call_id':'head','content':'Full result head only'}]
    reviewing = finish(runtime,executor,'Generated result.json; review the declared snapshot.')
    return reviewing, executor, runtime[2][-1], snapshot, content


def read_artifacts(task, run):
    source=run['artifact_evidence_source'];offset=0;chunks=[]
    while True:
        page=delivered(task,{'action':'read','run_id':run['run_id'],
            'task_id':source['task_id'],'source_kind':'artifact_evidence',
            'source_sha256':source['source_sha256'],'offset':offset},call_id='artifact-'+str(offset))
        chunks.append(page['content'])
        if page['next_offset'] is None:return ''.join(chunks)
        offset=page['next_offset']


def test_head_only_tool_output_hands_off_the_complete_declared_generation(runtime,tmp_path):
    from lib.evolution import source_tools
    from lib.evolution.stage_launch import read_launch
    run,executor,reviewer,snapshot,original=artifact_review(runtime,tmp_path)
    assert snapshot['state']=='sealed' and run['artifact_evidence_source']['complete']
    assert reviewer['config']['toolSpecAllowlist']==['evolution_source', 'evolution_review']
    frozen=read_launch(run['stage_launch'],run_id=run['run_id'],user_id=OWNER)
    assert 'generated_artifact_source' in frozen['prompt'] and len(frozen['prompt'])<24000
    assert original not in frozen['prompt']
    (tmp_path/'result.json').write_text('Later unreviewed replacement')
    rows=[json.loads(line) for line in read_artifacts(reviewer,run).splitlines()]
    assert rows[0]['content']==original and rows[0]['content_sha256']==snapshot['content_sha256']
    assert source_tools.fully_read(reviewer,run['artifact_evidence_source'])
    (tmp_path/'result.json').unlink()
    assert json.loads(read_artifacts(reviewer,run))['content']==original
    settled=finish(runtime,reviewer,review());assert settled['status']=='completed'


def test_read_produced_but_not_received_artifact_cannot_pass(runtime,tmp_path):
    from lib.evolution.source_tools import execute
    run,_,reviewer,_,_=artifact_review(runtime,tmp_path,'short complete artifact')
    source=run['artifact_evidence_source']
    execute(reviewer,{'action':'read','run_id':run['run_id'],'task_id':source['task_id'],
        'source_kind':'artifact_evidence','source_sha256':source['source_sha256']})
    final=finish(runtime,reviewer,review())
    assert final['status']=='needs_attention' and final['review']['outcome']=='failure'
    assert next(c for c in final['review']['criterion_results'] if c['criterion_id']=='objective')['outcome']=='unknown'


def test_generation_redeclaration_is_idempotent_and_old_bytes_remain(runtime,tmp_path):
    from lib.evolution.artifact_evidence import declare
    run,executor=project_run(runtime,tmp_path);p=tmp_path/'claim.txt';p.write_text('first')
    first=declare(executor,{'path':'claim.txt'});again=declare(executor,{'path':'claim.txt'});assert again==first
    p.write_text('changed');second=declare(executor,{'path':'claim.txt'});assert second['artifact_id']!=first['artifact_id']
    run=finish(runtime,executor,'Two declared generations retained.');reviewer=runtime[2][-1]
    data=[json.loads(line) for line in read_artifacts(reviewer,run).splitlines()]
    assert [row['content'] for row in data]==['first','changed']


def test_binary_and_empty_files_preserve_exact_original_bytes(runtime,tmp_path):
    from lib.evolution.artifact_evidence import declare
    run,executor=project_run(runtime,tmp_path);raw=b'\x00\xff\x80\x01complete\x00';(tmp_path/'image.bin').write_bytes(raw);(tmp_path/'empty.txt').write_bytes(b'')
    binary=declare(executor,{'path':'image.bin'});declare(executor,{'path':'empty.txt'})
    run=finish(runtime,executor,'Byte sources only; semantic interpretation is unverified.')
    data=[json.loads(line) for line in read_artifacts(runtime[2][-1],run).splitlines()]
    assert data[0]['encoding']=='base64' and base64.b64decode(data[0]['content'])==raw
    assert data[0]['content_sha256']==hashlib.sha256(raw).hexdigest()==binary['content_sha256']
    assert data[1]['content']=='' and data[1]['bytes']==0


@pytest.mark.parametrize('path',['../outside','/etc/passwd','a/../claim.txt','./claim.txt','.env','data/record.json','a\\b','secrets.json'])
def test_outside_runtime_and_secret_paths_refuse_before_store_writes(runtime,tmp_path,path):
    from lib.evolution.artifact_evidence import declare
    run,executor=project_run(runtime,tmp_path);writes=runtime[1].writes
    with pytest.raises((EvolutionStateError,ValueError,OSError)):declare(executor,{'path':path})
    assert runtime[1].writes==writes


@pytest.mark.parametrize('kind',['file_symlink','directory_symlink','hardlink','fifo'])
def test_nonregular_and_escaping_links_refuse(runtime,tmp_path,kind):
    from lib.evolution.artifact_evidence import declare
    run,executor=project_run(runtime,tmp_path);outside=tmp_path.parent/(tmp_path.name+'-outside');outside.mkdir();(outside/'value').write_text('foreign')
    if kind=='file_symlink':(tmp_path/'value').symlink_to(outside/'value');path='value'
    elif kind=='directory_symlink':(tmp_path/'link').symlink_to(outside,target_is_directory=True);path='link/value'
    elif kind=='hardlink':os.link(outside/'value',tmp_path/'value');path='value'
    else:os.mkfifo(tmp_path/'pipe');path='pipe'
    writes=runtime[1].writes
    with pytest.raises((EvolutionStateError,ValueError,OSError)):declare(executor,{'path':path})
    assert runtime[1].writes==writes


def test_changed_file_during_capture_refuses(runtime,tmp_path,monkeypatch):
    from lib.evolution import artifact_evidence as evidence
    run,executor=project_run(runtime,tmp_path);p=tmp_path/'value';p.write_bytes(b'a'*100000);read=os.read;changed=[False];target_inode=p.stat().st_ino
    def changing(fd,n):
        chunk=read(fd,n)
        if not changed[0] and os.fstat(fd).st_ino==target_inode:changed[0]=True;p.write_bytes(b'b'*100001)
        return chunk
    monkeypatch.setattr(evidence.os,'read',changing)
    with pytest.raises(EvolutionStateError,match='changed during capture'):evidence.declare(executor,{'path':'value'})
    assert changed[0]


def test_failed_partial_capture_preserves_cost_but_never_accepts_missing_content(runtime,tmp_path,monkeypatch):
    from lib.evolution import artifact_evidence as evidence
    from lib.evolution import stage_outcomes
    from lib.storage.errors import StorageError
    run,executor=project_run(runtime,tmp_path);(tmp_path/'result').write_text('complete '*10000)
    put=stage_outcomes._put_once;seen=[0]
    def fail(namespace,*args,**kwargs):
        if namespace==evidence.PARTS:
            seen[0]+=1
            if seen[0]==2:raise StorageError('unavailable','artifact part failed')
        return put(namespace,*args,**kwargs)
    with monkeypatch.context() as m:
        m.setattr(stage_outcomes,'_put_once',fail)
        with pytest.raises(StorageError):evidence.declare(executor,{'path':'result'})
    run=finish(runtime,executor,'Snapshot capture failed; original content remains unverified.')
    assert run['artifact_evidence_source']['complete'] is False and run['actuals']['tokens']==15
    data=json.loads(read_artifacts(runtime[2][-1],run));assert data['state']=='unavailable' and data['content'] is None
    final=finish(runtime,runtime[2][-1],review());assert final['status']=='needs_attention'
    assert final['actuals']['tokens']==30 and final['actuals'].get('tokens')!=0


def test_all_declared_generations_survive_terminal_closure(runtime,tmp_path):
    from lib.evolution import artifact_evidence as evidence
    _,executor=project_run(runtime,tmp_path);p=tmp_path/'value'
    for i in range(33):
        p.write_text(str(i));evidence.declare(executor,{'path':'value'})
    p.unlink()
    run=finish(runtime,executor,'All generations retained.')
    rows=[json.loads(line) for line in read_artifacts(runtime[2][-1],run).splitlines()]
    assert [row['content'] for row in rows]==[str(i) for i in range(33)]


def test_reviewer_foreign_owner_and_stale_executor_cannot_declare(runtime,tmp_path):
    from lib.evolution.artifact_evidence import declare
    run,executor,reviewer,_,_=artifact_review(runtime,tmp_path,'complete')
    for task in [reviewer,executor,{**executor,'_userId':OWNER+1}]:
        with pytest.raises(EvolutionStateError):declare(task,{'path':'result.json'})


def test_concurrent_generations_share_the_owned_manifest(runtime,tmp_path):
    from lib.evolution.artifact_evidence import declare
    run,executor=project_run(runtime,tmp_path)
    for i in range(4):(tmp_path/str(i)).write_text(str(i))
    with ThreadPoolExecutor(max_workers=4) as pool:values=list(pool.map(lambda i:declare(executor,{'path':str(i)}),range(4)))
    run=finish(runtime,executor,'Four snapshots declared.');rows=[json.loads(x) for x in read_artifacts(runtime[2][-1],run).splitlines()]
    assert {r['artifact_id'] for r in rows}=={r['artifact_id'] for r in values}


def test_owned_terminal_capsule_recovers_artifact_after_task_and_live_file_loss(runtime,tmp_path,monkeypatch):
    from lib.evolution import stage_outcomes
    from lib.tasks_pkg.manager import _persist,_terminal
    from lib.agent_core.execution_session import ExecutionPhase
    from tests.evolution.test_evolution_stage_outcomes import terminal
    from lib.evolution.artifact_evidence import declare
    run,executor=project_run(runtime,tmp_path);original='Full independently readable original 🧪'*1800
    p=tmp_path/'result';p.write_text(original);declare(executor,{'path':'result'})
    terminal(executor,'Created result; only its head is in tool output.')
    executor['_executionSession'].settle(ExecutionPhase.COMPLETED,cause='controlled_test')
    with monkeypatch.context() as m:
        m.setattr(_terminal,'notify_terminal_listeners',lambda _:(_ for _ in ()).throw(RuntimeError('lost after result commit')))
        assert _persist.persist_task_result(executor,_defer_heavy_release=True) is False
    p.unlink();monkeypatch.setattr(runtime[0],'default_task_loader',lambda *a,**kw:None)
    resumed=runtime[0].advance_run(run['run_id'],user_id=OWNER)
    assert resumed['status']=='reviewing' and len(runtime[2])==2
    assert json.loads(read_artifacts(runtime[2][-1],resumed))['content']==original
    assert resumed['episode_id']==run['episode_id'] and resumed['attempt']==run['attempt']
    meta=stage_outcomes.read_metadata(run['run_id'],executor['id'],user_id=OWNER)
    assert meta['artifact_evidence']['complete'] and meta['actuals']['tokens']==15
    assert runtime[0].advance_run(run['run_id'],user_id=OWNER)['task_id']==resumed['task_id']


def test_registry_exposes_declaration_only_to_current_project_executor(runtime,tmp_path):
    from lib.tools.registry import ToolContext,assemble_tool_list
    run,executor=project_run(runtime,tmp_path)
    def names(task):
        context=ToolContext(cfg=task['config'],task_id=task['id'],
            project_path=str(tmp_path),project_enabled=True,search_mode='off',search_enabled=False,
            fetch_enabled=False,code_exec_enabled=False,browser_enabled=False,desktop_enabled=False)
        tools,_=assemble_tool_list(context)
        return {tool['function']['name'] for tool in tools}
    assert 'evolution_artifact' in names(executor)
    run=finish(runtime,executor,'Plain output without declared files')
    assert 'evolution_artifact' not in names(runtime[2][-1])


def test_scientific_project_data_is_allowed_but_actual_runtime_storage_is_not(runtime,tmp_path,monkeypatch):
    from lib.evolution.artifact_evidence import declare
    run,executor=project_run(runtime,tmp_path);data=tmp_path/'data';data.mkdir();(data/'observations.csv').write_text('trial,result\n1,unknown\n')
    snapshot=declare(executor,{'path':'data/observations.csv'});assert snapshot['state']=='sealed'
    with monkeypatch.context() as m:
        m.setattr('runtime_path_policy.get_runtime_base',lambda:str(tmp_path))
        before=runtime[1].writes
        with pytest.raises(EvolutionStateError,match='runtime storage'):declare(executor,{'path':'data/observations.csv'})
        assert runtime[1].writes==before


def test_program_archive_and_semantic_windows_keep_artifact_body_and_complete_identity(runtime,tmp_path):
    from lib.evolution import programs,artifact_evidence,semantic_memory,records
    from tests.evolution.test_evolution_programs import step
    program=programs.create_program({'objective':'Retain complete phase artifacts.',
        'project_path':str(tmp_path),'acceptance':['Artifacts remain independently readable.'],
        'steps':[step(),step('final','Audit retained evidence')]},user_id=OWNER,idempotency_key='artifact-archive-control')
    programs.command_program(program['program_id'],{'action':'resume'},user_id=OWNER)
    programs.advance_program(program['program_id'],user_id=OWNER)
    program=programs.advance_program(program['program_id'],user_id=OWNER)
    executor=runtime[2][-1];original='Original phase 🧪 network saturated\n'*1600
    (tmp_path/'phase.txt').write_text(original)
    artifact_evidence.declare(executor,{'path':'phase.txt'})
    run=finish(runtime,executor,'Declared phase.txt; body lives in the snapshot.')
    read_artifacts(runtime[2][-1],run)
    judged=json.loads(review());judged['requirement_results']=[
        {'requirement_id':f'requirement-{i+1}','outcome':'pass','evidence_ref':'Controlled snapshot was received.'}
        for i,_ in enumerate(run['acceptance_requirements'])]
    assert finish(runtime,runtime[2][-1],json.dumps(judged))['status']=='completed'
    program=programs.advance_program(program['program_id'],user_id=OWNER)
    receipt=programs.experiment_page(program['program_id'],user_id=OWNER)['experiments'][0]
    assert receipt['artifact_evidence_source']==run['artifact_evidence_source']
    (tmp_path/'phase.txt').unlink();args={'source_kind':'artifact_evidence'};parts=[]
    while True:
        chunks,cursor,_=semantic_memory.window(program,args,OWNER)
        parts.extend(chunk['text'] for chunk in chunks)
        if cursor is None:break
        args={**cursor,'source_kind':'artifact_evidence'}
    assert json.loads(''.join(parts))['content']==original
    receipt['artifact_evidence_source']['complete']=False
    records.put_record(programs.EXPERIMENTS,programs._experiment_key(program['program_id'],1,OWNER),receipt,kind='experiment')
    with pytest.raises(EvolutionStateError,match='reference changed'):
        semantic_memory.window(program,{'source_kind':'artifact_evidence'},OWNER)


def test_artifact_reviewer_can_read_short_inline_delivery_and_tool_sources(runtime,tmp_path):
    from lib.evolution import source_tools
    run,executor,reviewer,_,_=artifact_review(runtime,tmp_path,'Small generated file')
    assert 'result_source' not in run and 'tool_evidence_source' not in run
    listed=source_tools.execute(reviewer,{'action':'list'})['sources']
    assert {source.get('source_kind','deliverable') for source in listed}=={'deliverable','tool_evidence','artifact_evidence'}
    for source in listed:
        kind=source.get('source_kind','deliverable')
        page=delivered(reviewer,{'action':'read','run_id':run['run_id'],'task_id':executor['id'],
            'source_sha256':source['source_sha256'],'source_kind':kind},call_id='inline-'+kind)
        assert source_tools.fully_read(reviewer,{k:source[k] for k in source if k not in {'stage','archive_state','terminal_verdict'}})
        if kind=='deliverable':assert page['content']=='Generated result.json; review the declared snapshot.'
        if kind=='tool_evidence':assert 'Full result head only' in page['content']
    with pytest.raises(EvolutionStateError,match='outside'):
        source_tools.execute(reviewer,{'action':'read','run_id':run['run_id'],'task_id':'foreign',
            'source_sha256':listed[-1]['source_sha256'],'source_kind':'tool_evidence'})


def test_large_complete_source_survives_live_loss_and_owner_paged_recovery(runtime,tmp_path):
    from lib.evolution import artifact_evidence as evidence, stage_outcomes
    _,executor=project_run(runtime,tmp_path)
    # Escaping doubles the terminal JSONL body: exceed all historical byte and part ceilings.
    original='\n'*(13*1024*1024)+'complete tail 🧪'
    p=tmp_path/'large.txt';p.write_text(original,encoding='utf-8')
    snapshot=evidence.declare(executor,{'path':'large.txt'})
    assert snapshot['bytes']==len(original.encode())
    assert snapshot['content_parts']>420 and snapshot['entry_bytes']>16*1024*1024
    p.unlink()
    run=finish(runtime,executor,'Complete large source retained.')
    source=run['artifact_evidence_source']
    assert source['complete']
    descriptor=stage_outcomes.read_metadata(run['run_id'],executor['id'],user_id=OWNER)['artifact_evidence']
    assert descriptor['content_parts']>560
    offset=0;pages=[]
    while True:
        page=stage_outcomes.page(run['run_id'],executor['id'],user_id=OWNER,
            source_kind='artifact_evidence',offset=offset)
        pages.append(page['content'])
        if page['next_offset'] is None:break
        offset=page['next_offset']
    text=''.join(pages)
    assert hashlib.sha256(text.encode()).hexdigest()==source['source_sha256']
    row=json.loads(text)
    assert row['content']==original and row['content_sha256']==hashlib.sha256(original.encode()).hexdigest()
    with pytest.raises(EvolutionStateError):
        stage_outcomes.page(run['run_id'],executor['id'],user_id=OWNER+1,source_kind='artifact_evidence')
    print(json.dumps({'original_bytes':snapshot['bytes'],'snapshot_parts':snapshot['content_parts'],
        'terminal_jsonl_bytes':len(text.encode()),'terminal_parts':descriptor['content_parts']}))
