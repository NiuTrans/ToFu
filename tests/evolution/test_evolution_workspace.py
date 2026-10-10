"""Studio projections preserve model identity, owner scope and live human waits."""
from types import SimpleNamespace
import pytest
pytestmark = pytest.mark.unit


def test_model_choices_use_owner_eligibility_and_auto_pins_highest_declared_quality(monkeypatch):
    from lib.evolution import workspace
    observed=[]
    document={'models':[{'creator_id':'a','model_id':'small','display_name':'Small','quality_rank':1},
                        {'creator_id':'a','model_id':'large','display_name':'Large','quality_rank':9}]}
    repo=SimpleNamespace(get=lambda boundary:(observed.append(boundary.owner_user_id) or SimpleNamespace(document=document)))
    monkeypatch.setattr('lib.model_routing.ModelRoutingRepository',lambda:repo)
    monkeypatch.setattr('lib.model_routing.controls.reasoning_controls',lambda doc:[
        {'creator_id':'a','model_id':name,'provider_id':'','offering_id':'','availability':'available'}
        for name in ('small','large')]+[{'availability':'no_route'}])
    assert workspace.resolve_run_model(None,user_id=97)=={'creator_id':'a','model_id':'large'}
    assert observed==[97]
    monkeypatch.setattr(workspace,'model_options',lambda **kw:[])
    with pytest.raises(RuntimeError,match='No available model route'):
        workspace.resolve_run_model(None,user_id=97)


def test_human_questions_project_only_active_owner_execution_waits(monkeypatch):
    from lib.evolution.workspace import pending_questions
    task={'toolRounds':[{'status':'done','guidanceId':'old'},
        {'status':'awaiting_human','guidanceId':'new','guidanceQuestion':'Choose scope',
         'guidanceType':'batch','guidanceBatchQuestions':[{'question':'Scope?','response_type':'free_text'}]}]}
    calls=[]
    monkeypatch.setattr('lib.evolution.bridge.default_task_loader',lambda tid,**kw:(calls.append((tid,kw['user_id'])) or task))
    run={'status':'executing','task_id':'task'}
    result=pending_questions(run,user_id=98)
    assert calls==[('task',98)]
    assert [q['guidance_id'] for q in result]==['new']
    assert result[0]['questions'][0]['question']=='Scope?'
    assert pending_questions({**run,'status':'cancelled'},user_id=98)==[]
    assert pending_questions({**run,'status':'reviewing'},user_id=98)==[]


def test_list_transport_budget_excludes_large_delivery_and_learning(monkeypatch):
    import json
    from lib.evolution.workspace import run_summaries
    run={'run_id':'r','objective':'目标'*2000,'status':'completed','task_id':'t','attempt':2,
         'created_at':1,'updated_at':2,'result':'x'*24000,'learning':{'large':'x'*900000}}
    monkeypatch.setattr('lib.evolution.runs.list_run_snapshots',lambda **kw:[dict(run,run_id=f'r{i}') for i in range(50)])
    result=run_summaries(user_id=99)
    assert len(result)==50
    assert all(len(row['objective'])==180 and 'result' not in row and 'learning' not in row for row in result)
    assert len(json.dumps(result,ensure_ascii=False).encode()) < 40_000


def test_studio_art_encoded_and_decoded_budget():
    import struct
    from pathlib import Path
    asset=Path(__file__).resolve().parents[2]/'frontend/src/features/evolution/assets/office-clear.webp'
    data=asset.read_bytes()
    assert len(data) <= 512 * 1024
    assert data[:4] == b'RIFF' and data[8:16] == b'WEBPVP8 '
    width,height=(value & 0x3fff for value in struct.unpack('<HH',data[26:30]))
    assert width * height * 4 <= 8 * 1024 * 1024


def test_office_scene_total_art_memory_budget():
    import struct
    from pathlib import Path
    assets=Path(__file__).resolve().parents[2]/'frontend/src/features/evolution/assets'
    images=[(assets/name).read_bytes() for name in ('office-clear.webp','people.png')]
    # Six-character sprite atlas (3 roles + 3 swarm visitors) landed with the
    # office scene; budgets are pinned at the measured totals with headroom.
    assert sum(map(len,images)) <= 5 * 1024 * 1024
    office_size=struct.unpack('<HH',images[0][26:30])
    people_size=struct.unpack('>II',images[1][16:24])
    decoded=sum((w & 0x3fff)*(h & 0x3fff)*4 for w,h in (office_size,people_size))
    assert decoded <= 27 * 1024 * 1024
    # Renderer ceiling 1440x1080 RGBA, separate from the atlas/background.
    assert decoded + 1440*1080*4 <= 34 * 1024 * 1024


def test_floor_activity_merges_live_work_across_engines(monkeypatch):
    from lib.evolution import workspace
    evolution_rows = [
        {'run_id': 'evo-live', 'objective': '研究记忆方案', 'status': 'executing',
         'updated_at': 30},
        {'run_id': 'evo-done', 'objective': '已完成', 'status': 'completed',
         'updated_at': 99},
    ]
    orchestration_rows = [
        {'id': 'flow-1', 'orch_id': 'def-9', 'name': '周报表流程',
         'status': 'running', 'updated_at': 50},
        {'id': 'goal-1', 'orch_id': 'chat-goal:conv-7', 'name': 'Goal Mode',
         'status': 'running', 'updated_at': 40},
        {'id': 'flow-done', 'orch_id': 'def-9', 'name': '旧流程',
         'status': 'done', 'updated_at': 90},
        {'id': 'flow-paused', 'orch_id': 'def-8', 'name': '待批流程',
         'status': 'paused', 'updated_at': 10},
    ]
    floor = workspace.floor_activity(
        evolution_rows, user_id=101,
        orchestration_lister=lambda uid: orchestration_rows)
    kinds = {item['run_id']: item['kind'] for item in floor['items']}
    assert kinds == {'flow-1': 'flow', 'goal-1': 'goal',
                     'evo-live': 'evolution', 'flow-paused': 'flow'}
    assert floor['items'][0]['run_id'] == 'flow-1'
    goal = next(item for item in floor['items'] if item['run_id'] == 'goal-1')
    assert goal['conversation_id'] == 'conv-7'
    assert floor['degraded'] is False


def test_floor_activity_orchestration_read_failure_degrades_explicitly():
    from lib.evolution import workspace
    def _boom(uid):
        raise RuntimeError('storage hiccup')
    floor = workspace.floor_activity(
        [{'run_id': 'evo-live', 'objective': 'x', 'status': 'reviewing',
          'updated_at': 5}],
        user_id=102, orchestration_lister=_boom)
    assert floor['degraded'] is True
    assert [item['kind'] for item in floor['items']] == ['evolution']


def test_runs_panel_shares_one_evolution_read_and_bounds_transport(monkeypatch):
    import json
    from lib.evolution import workspace
    calls = []
    rows = [{'run_id': f'r{i}', 'objective': '目标' * 400, 'status': 'executing',
             'task_id': 't', 'attempt': 1, 'created_at': i, 'updated_at': i}
            for i in range(50)]
    monkeypatch.setattr('lib.evolution.runs.list_run_snapshots',
                        lambda **kw: calls.append(kw) or rows)
    panel = workspace.runs_panel(user_id=103,
                                 orchestration_lister=lambda uid: [])
    assert len(calls) == 1
    assert len(panel['runs']) == 50
    assert len(panel['floor']['items']) == 16
    assert all(len(item['title']) <= 180 for item in panel['floor']['items'])
    assert len(json.dumps(panel, ensure_ascii=False).encode()) < 80_000


def test_gate_pending_projects_only_awaiting_decisions_bounded():
    from lib.evolution import workspace
    rows = [
        {'mutation_id': 'm-active', 'kind': 'macro_tool', 'status': 'active',
         'version': 3, 'payload': {'title': '已启用'}, 'updated_at': 99},
        {'mutation_id': 'm-prop', 'kind': 'skill_candidate', 'status': 'proposed',
         'version': 1, 'payload': {'name': '引用校验技能'},
         'evidence_episode_ids': ['e1', 'e2'], 'updated_at': 20},
        {'mutation_id': 'm-conf', 'kind': 'budget_policy', 'status': 'confirmed',
         'version': 2, 'payload': {}, 'evidence_episode_ids': ['e3'],
         'updated_at': 40},
    ]
    pending = workspace.gate_pending(
        user_id=104, mutations_lister=lambda uid: rows)
    assert pending['degraded'] is False
    assert [i['mutation_id'] for i in pending['items']] == ['m-conf', 'm-prop']
    first = pending['items'][0]
    assert first['title'] == 'budget_policy' and first['evidence_count'] == 1
    assert pending['items'][1]['title'] == '引用校验技能'
    assert set(first) == {'mutation_id', 'kind', 'status', 'version', 'title',
                          'evidence_count', 'updated_at'}


def test_gate_pending_store_failure_degrades_explicitly():
    from lib.evolution import workspace
    def _boom(uid):
        raise RuntimeError('store down')
    pending = workspace.gate_pending(user_id=105, mutations_lister=_boom)
    assert pending == {'items': [], 'degraded': True}


def test_goal_workspace_projects_budget_envelope_for_the_office_gauge(monkeypatch):
    from lib.evolution import workspace
    monkeypatch.setattr('lib.evolution.goals.get_goal',
                        lambda gid, **kw: {'goal_id': gid,
                                           'budget_envelope': {
                                               'max_tokens': 50000,
                                               'max_wall_clock_seconds': 600,
                                               'max_tool_calls': 30},
                                           'criteria': []})
    monkeypatch.setattr('lib.evolution.goals.get_plan', lambda pid, **kw: {})
    monkeypatch.setattr('lib.evolution.library.list_materials',
                        lambda rid, **kw: [])
    result = workspace.goal_workspace(
        {'goal_id': 'g', 'plan_id': 'p', 'run_id': 'r'}, user_id=106)
    assert result['budget'] == {'max_tokens': 50000,
                                'max_wall_clock_seconds': 600,
                                'max_tool_calls': 30}
    monkeypatch.setattr('lib.evolution.goals.get_goal', lambda gid, **kw: {})
    assert workspace.goal_workspace(
        {'goal_id': 'g', 'plan_id': 'p', 'run_id': 'r'},
        user_id=106)['budget'] is None


def test_floor_normalizes_mixed_timestamp_units_before_sorting():
    from lib.evolution import workspace
    # Evolution rows stamp seconds; tofu-db run rows stamp milliseconds.
    floor = workspace.floor_activity(
        [{'run_id': 'evo-new', 'objective': '新进化', 'status': 'executing',
          'updated_at': 1_790_000_000}],
        user_id=107,
        orchestration_lister=lambda uid: [
            {'id': 'flow-old', 'orch_id': 'd', 'name': '旧流程',
             'status': 'running', 'updated_at': 1_789_999_000_000},
            {'id': 'flow-unnamed', 'orch_id': 'd', 'name': '',
             'status': 'running', 'updated_at': 1_790_000_100_000},
        ])
    assert [item['run_id'] for item in floor['items']] == [
        'flow-unnamed', 'evo-new', 'flow-old']
    assert floor['items'][0]['title'] == 'flow-unnamed'


def test_gate_pending_preserves_activation_intent_without_payload():
    from lib.evolution.workspace import gate_pending
    intent={'started_at':2,'context_fingerprint':'0'*64}
    row={'mutation_id':'pending','kind':'budget_policy','status':'confirmed','version':1,
        'activation_intent':intent,'payload':{'policy':'private policy'},'updated_at':2}
    result=gate_pending(user_id=104,mutations_lister=lambda uid:[row])
    item=result['items'][0]
    assert item['activation_intent']==intent and 'payload' not in item
    assert item['activation_intent'] is not intent
