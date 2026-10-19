"""Goal material ownership, URL safety, bounded growth, CAS and task authority."""
import pytest
from lib.evolution import library
from lib.evolution.contracts import EvolutionContractError, EvolutionStateError
pytestmark=pytest.mark.unit

@pytest.fixture
def shelf(monkeypatch):
    from tests.support.evolution_store import install_store
    store=install_store(monkeypatch)
    from lib.evolution import records
    for rid in ('a','b'):store.rows[(records.NAMESPACE_RUNS,'123:'+rid)]={'run_id':rid}
    monkeypatch.setattr('lib.evolution.runs.get_run',lambda rid,*,user_id:
        {'run_id':rid,'status':'executing','task_id':'task-'+rid} if user_id==123 and rid in ('a','b') else None)
    return store

def material(**kw):
    return {'kind':'paper','title':'Relevant research','url':'https://example.org/paper','note':'Supports the acceptance check',**kw}

def test_goal_and_owner_isolation_and_idempotent_link(shelf):
    first=library.save_material('a',material(),user_id=123)
    second=library.save_material('a',material(title='Updated title'),user_id=123)
    assert first['material_id']==second['material_id']
    assert len(library.list_materials('a',user_id=123))==1
    assert library.list_materials('b',user_id=123)==[]
    with pytest.raises(EvolutionStateError):library.list_materials('a',user_id=456)
    with pytest.raises(EvolutionStateError):library.save_material('missing',material(),user_id=123)
    library.remove_material('a',first['material_id'],user_id=123)
    assert library.list_materials('a',user_id=123)==[]

@pytest.mark.parametrize('url',['javascript:alert(1)','file:///tmp/a','https://u:p@example.org/','https://example.org/\n','https://example.org\\@evil.test','https://example.org:invalid/a'])
def test_unsafe_links_rejected_without_write(shelf,url):
    with pytest.raises(EvolutionContractError):library.save_material('a',material(url=url),user_id=123)
    assert shelf.writes==0

def test_materials_cross_old_count_limit_without_eviction(shelf):
    for i in range(129):library.save_material('a',material(url=f'https://example.org/{i}'),user_id=123)
    assert len(library.list_materials('a',user_id=123))==129
    library.save_material('a',material(url='https://example.org/1',title='updated'),user_id=123)
    rows=library.list_materials('a',user_id=123)
    assert len(rows)==129 and rows[0]['url']=='https://example.org/0'
    assert rows[1]['title']=='updated'


def test_cas_retry_preserves_a_concurrent_writer(shelf,monkeypatch):
    original=shelf.command;once=True
    def command(op,payload,command_id):
        nonlocal once
        if once:
            once=False
            library.save_material('a',material(url='https://example.org/other'),user_id=123)
        return original(op,payload,command_id)
    monkeypatch.setattr(shelf,'command',command)
    library.save_material('a',material(),user_id=123)
    assert len(library.list_materials('a',user_id=123))==2

def test_tool_uses_live_task_owner_and_binding(shelf):
    task={'id':'task-a','_userId':123,'config':{'evolution':{'run_id':'a','stage':'executing'}}}
    def invoke():return library.handle_material(task,None,'evolution_material','call',material(),1,{}, {},'',False)
    assert invoke()[0]['fetched'] is True
    assert library.list_materials('a',user_id=123)[0]['origin']=='agent'
    task['id']='stale-task';assert invoke()[0]['fetched'] is False
    task['id']='task-a';task['config']['evolution']['stage']='reviewing';assert invoke()[0]['fetched'] is False
    task['config']={};assert invoke()[0]['fetched'] is False

def test_tool_only_exposed_in_run_execution_even_with_execution_limits():
    from tests.tools.test_tool_registry import _ctx,_names
    from lib.tools.registry import assemble_tool_list
    for binding,expected in [({},False),({'run_id':'a','goal_id':'a','stage':'reviewing'},False),({'run_id':'a','goal_id':'a','stage':'executing'},True)]:
        ctx=_ctx(cfg={'evolution':binding,'executionLimits':{'max_tool_calls':10}})
        assemble_tool_list(ctx)
        assert ('evolution_material' in _names(ctx.executable_tool_catalog)) is expected


def test_dense_materials_cross_old_aggregate_budgets_atomically(shelf):
    import json
    from lib.evolution import records
    for i in range(160):
        library.save_material('a',material(title='研'*240,url='https://example.org/'+str(i)+'研'*1900,note='研'*500),user_id=123)
    assert len(library.list_materials('a',user_id=123))==160
    row=records.get_record(records.NAMESPACE_LIBRARIES,'123:a')
    assert len(json.dumps(row,ensure_ascii=False).encode())>1024*1024
    assert len(row['coordination']['updates'])==160
    assert all(event['kind']=='material_saved' for event in row['coordination']['updates'])



def test_agent_can_read_user_materials_with_bounded_pagination(shelf):
    for i in range(10):library.save_material('a',material(url=f'https://example.org/{i}'),user_id=123)
    task={'id':'task-a','_userId':123,'config':{'evolution':{'run_id':'a','stage':'executing'}}}
    result=library.handle_material(task,None,'evolution_material','call',{'action':'list'},1,{}, {},'',False)
    import json
    page=json.loads(result[0]['snippet'])
    assert len(page['materials'])==8 and page['total']==10 and page['next_cursor']==8
