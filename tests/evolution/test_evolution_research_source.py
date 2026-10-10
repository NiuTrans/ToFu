"""Prior discovery seeds a programme without changing mission or proof rights."""
from copy import deepcopy
import json
import pytest
from tests.evolution.test_evolution_programs import runtime as runtime, OWNER, launch
from tests.evolution.test_evolution_exploration_tools import bound_task, call
from lib.evolution import programs
from lib.evolution.contracts import EvolutionStateError
pytestmark = pytest.mark.unit


@pytest.fixture
def original(runtime,monkeypatch):
    from lib.research import records
    monkeypatch.setattr(records,'_storage',lambda **kwargs:runtime[1])
    result = {'accepted':[{'id':'selected','title':'Original proposed mechanism'}],
              'survey_md':'Original survey ' * 1000,'open_gaps':{'open_gaps':[{'id':'gap'}]}}
    records.persist_record({'task_id':'original-discovery','user_id':OWNER,'direction':'Original direction',
        'lang':'en','status':'done','result':result})
    return records, result


def request():
    return {'objective':'Complete the original research objective with decisive measured experiments',
            'research_source':{'task_id':'original-discovery','idea_id':'selected'}}


def test_program_keeps_default_scientific_frontier_and_original_mission(original):
    programme = programs.create_program(request(),user_id=OWNER,idempotency_key='source-program')
    assert programme['objective'] == request()['objective']
    assert programme['status'] == 'ready' and programme['actuals']['tokens'] == 0
    assert programme['research_source']['task_id'] == 'original-discovery'
    assert any(step.get('requires_scientific_protocol') for step in programme['queue'])
    assert programs.create_program(request(),user_id=OWNER,idempotency_key='source-program') == programme
    context = json.loads(programs._context(programme,OWNER))
    assert context['research_source']['read_action'] == 'read_research_source'


def test_current_executor_reads_complete_original_pages_with_digest(runtime,original):
    programme = launch(runtime,programs.create_program(request(),user_id=OWNER,idempotency_key='read-original'))
    task = bound_task(runtime,programme)
    pages = []; offset = 0; digest = programme['research_source']['source_digest']
    while True:
        page = call(task,programme,'read_research_source',source_digest=digest,offset=offset)
        assert page['source_digest'] == digest
        pages.append(page['content'])
        if page['next_offset'] is None:break
        assert page['next_offset'] > offset
        offset = page['next_offset']
    result = json.loads(''.join(pages))
    assert result['survey_md'] == original[1]['survey_md']
    assert result['selected_idea']['id'] == 'selected'
    assert len(pages) > 1
    assert 'unverified' in page['evidence_status']


def test_changed_original_refuses_read_instead_of_latest_fallback(original):
    records, result = original
    programme = programs.create_program(request(),user_id=OWNER,idempotency_key='changed-source')
    records.persist_record({'task_id':'original-discovery','user_id':OWNER,'direction':'Original direction',
        'lang':'en','status':'done','result':{**result,'survey_md':'Changed original'}})
    with pytest.raises(EvolutionStateError,match='identity changed'):
        programs.read_research_source(programme,user_id=OWNER)
    with pytest.raises(EvolutionStateError):
        programs.create_program(request(),user_id=OWNER,idempotency_key='changed-source')


def test_other_owner_cannot_create_from_original(original):
    with pytest.raises(EvolutionStateError,match='owned research source unavailable'):
        programs.create_program(request(),user_id=OWNER+1,idempotency_key='foreign-source')


def test_stale_executor_cannot_read_initial_source(runtime,original):
    programme = launch(runtime,programs.create_program(request(),user_id=OWNER,idempotency_key='stale-source'))
    task = bound_task(runtime,programme)
    task['id'] = 'stale-task'
    with pytest.raises(EvolutionStateError):
        call(task,programme,'read_research_source')


@pytest.mark.parametrize('backend',['sqlite','tofudb'])
def test_selected_authority_retains_reference_and_serves_original(tmp_path,monkeypatch,backend):
    from lib.storage import StorageSupervisor
    from lib.research import records as research_records
    monkeypatch.setenv('TOFUDB_OWNER_ID','1')
    supervisor = StorageSupervisor(project_root=tmp_path/'storage',backend=backend,startup_timeout=60)
    supervisor.start()
    try:
        monkeypatch.setattr(programs.records,'_storage',lambda **kwargs:supervisor.client)
        monkeypatch.setattr(research_records,'_storage',lambda **kwargs:supervisor.client)
        research_records.persist_record({'task_id':'original-discovery','user_id':1,'direction':'Original direction',
            'lang':'en','status':'done','result':{'accepted':[{'id':'selected','title':'Original'}],
                'survey_md':'Retained original evidence','open_gaps':{}}})
        programme = programs.create_program(request(),user_id=1,idempotency_key='selected-store-source')
        retained = programs.get_program(programme['program_id'],user_id=1)
        assert retained['research_source'] == programme['research_source']
        page = programs.read_research_source(retained,user_id=1)
        assert json.loads(page['content'])['survey_md'] == 'Retained original evidence'
        assert page['next_offset'] is None
        assert len(json.dumps(retained['research_source'],separators=(',',':')).encode()) < 300
    finally:
        supervisor.stop()


def test_page_uses_one_atomic_source_snapshot(original,monkeypatch):
    records, _ = original
    programme = programs.create_program(request(),user_id=OWNER,idempotency_key='single-source-read')
    original_load = records.load_record
    calls = []
    def load(*args,**kwargs):
        calls.append((args,kwargs))
        return original_load(*args,**kwargs)
    monkeypatch.setattr(records,'load_record',load)
    programs.read_research_source(programme,user_id=OWNER)
    assert len(calls) == 1
    assert calls[0][1] == {'user_id':OWNER,'internal':True}
