"""Research action input admission is separate from cumulative usage."""
import pytest

pytestmark = pytest.mark.unit


def test_projection_preserves_exact_sources_but_removes_receipt_prose():
    from lib.research.context import project_workspace
    workspace = {'source_files': [{'path': 'main.tex', 'content': 'exact source'}],
        'runs': [{'tool_receipts': [{'result_excerpt': 'large prose', 'artifact_ref': 'a'}]}]}
    projected = project_workspace(workspace, 'compile')
    assert projected['source_files'] == workspace['source_files']
    assert 'result_excerpt' not in projected['runs'][0]['tool_receipts'][0]
    assert workspace['runs'][0]['tool_receipts'][0]['result_excerpt'] == 'large prose'
    assert 'source_files' not in project_workspace(workspace, 'experiment')


def test_large_context_fails_before_dispatch_and_counts_tools(monkeypatch):
    import lib.research.context as context
    monkeypatch.setattr(context, 'action_window', lambda _: 8192)
    assert context.admit_messages([{'role': 'user', 'content': 'brief'}], model='test') < 100
    with pytest.raises(ValueError, match='exact sources were not truncated'):
        context.admit_messages([{'role': 'user', 'content': 'brief'}], model='test',
            tools=[{'description': 'unique content ' * 30000}])


def test_actual_persisted_evidence_keys_reach_actions(monkeypatch):
    from lib.research.action import _research_context
    import lib.research.persistence as persistence
    monkeypatch.setattr(persistence, 'load_research_artifacts', lambda *a, **k: {
        'survey_md': 'Actual survey', 'open_gaps': {'open_gaps': [{'id': 'g'}]},
        'research_cycle': {'status': 'unresolved', 'issues': [{'change': 'measure'}]},
    })
    context = _research_context('topic', 'en', 1)
    assert context['survey_markdown'] == 'Actual survey'
    assert context['gap_map'] == [{'id': 'g'}]
    assert context['research_cycle']['status'] == 'unresolved'


@pytest.mark.parametrize('window,tokens,allowed',[(128000,100000,True),(32000,100000,False),
    (128000,117952,True),(128000,117953,False)])
def test_research_input_uses_model_capacity_with_output_and_safety_reserved(monkeypatch,window,tokens,allowed):
    from lib.research import context
    monkeypatch.setattr(context,'action_window',lambda model:window)
    monkeypatch.setattr('lib.token_counter.count_text',lambda *args,**kwargs:tokens)
    messages=[{'role':'user','content':'controlled token-count fixture'}]
    if allowed:assert context.admit_messages(messages,model='test',max_output=8000)==tokens
    else:
        with pytest.raises(ValueError,match='exact sources were not truncated'):
            context.admit_messages(messages,model='test',max_output=8000)


def test_explicit_input_limit_still_refuses_with_larger_model(monkeypatch):
    from lib.research import context
    monkeypatch.setattr(context,'action_window',lambda model:128000)
    monkeypatch.setattr('lib.token_counter.count_text',lambda *args,**kwargs:60000)
    with pytest.raises(ValueError,match='budget 48000'):
        context.admit_messages([],model='test',input_limit=48000)


@pytest.mark.parametrize('parameter,value',[('input_limit',0),('input_limit',True),
    ('input_limit',1.5),('max_output',-1),('max_output',True),('max_output',1.5)])
def test_invalid_reservations_are_rejected(monkeypatch,parameter,value):
    from lib.research import context
    monkeypatch.setattr(context,'action_window',lambda model:128000)
    monkeypatch.setattr('lib.token_counter.count_text',lambda *args,**kwargs:1)
    with pytest.raises(ValueError):context.admit_messages([],model='test',**{parameter:value})


def test_unknown_model_keeps_conservative_window(monkeypatch):
    from lib.research import context
    monkeypatch.setattr('lib.model_info._context.context_profile',lambda model:{})
    assert context.action_window('unknown')==32000


def test_bound_execution_context_keeps_original_selected_idea_beyond_preview(monkeypatch):
    from lib.research import action, records, persistence
    original = {'accepted':[None] + [{'id':f'idea-{n}'} for n in range(20)],
                'survey_md':'Original survey evidence', 'open_gaps':{'open_gaps':[{'id':'original-gap'}]}}
    calls = []
    def load(task_id,**kwargs):
        calls.append((task_id,kwargs))
        return original
    monkeypatch.setattr(records,'load_idea_source',load)
    monkeypatch.setattr(persistence,'load_research_artifacts',lambda *a,**k:pytest.fail('no latest-source fallback'))
    workspace = {'selected_idea_source_task_id':'original-task','selected_idea_id':'idea-19'}
    result = action._research_context('original direction','en',12,workspace)
    assert result['source_task_id'] == 'original-task'
    assert result['accepted'] == [{'id':'idea-19'}]
    assert result['survey_markdown'] == 'Original survey evidence'
    assert calls == [('original-task',{'user_id':12,'direction':'original direction','lang':'en','idea_id':'idea-19'})]


def test_bound_execution_source_failure_stops_message_construction(monkeypatch):
    from lib.research import action, records, persistence
    def missing(*args,**kwargs):
        raise ValueError('original discovery source unavailable')
    monkeypatch.setattr(records,'load_idea_source',missing)
    monkeypatch.setattr(persistence,'load_research_artifacts',lambda *a,**k:pytest.fail('no alternate evidence'))
    task = {'action':'experiment','direction':'scope','lang':'en','user_id':12}
    workspace = {'selected_idea_source_task_id':'missing-task','selected_idea_id':'idea'}
    with pytest.raises(ValueError,match='original discovery source unavailable'):
        action._messages(task,workspace,[])


def test_bound_context_retains_complete_survey_and_gaps_without_source_mutation(monkeypatch):
    from copy import deepcopy
    from lib.research import action, records
    original = {'accepted': [{'id': 'idea', 'linked_gap_id': 'gap-24'}],
                'survey_md': 's' * 20000,
                'open_gaps': {'open_gaps': [{'id': f'gap-{n}'} for n in range(30)]}}
    before = deepcopy(original)
    monkeypatch.setattr(records, 'load_idea_source', lambda *a, **k: original)
    result = action._research_context('topic', 'en', 12,
        {'selected_idea_source_task_id': 'source', 'selected_idea_id': 'idea'})
    assert result['survey_markdown'] == original['survey_md']
    assert result['gap_map'] == original['open_gaps']['open_gaps']
    projection = result['context_projection']
    assert projection['survey_total_chars'] == 20000
    assert projection['survey_omitted_chars'] == 0
    assert projection['gap_total_count'] == 30
    assert projection['gap_omitted_count'] == 0
    assert projection['gap_source_range'] == {'start': 0, 'end': 30}
    assert original == before


def test_complete_context_reports_no_omissions(monkeypatch):
    from lib.research import action, persistence
    monkeypatch.setattr(persistence, 'load_research_artifacts', lambda *a, **k:
        {'survey_md': 'complete', 'open_gaps': {'open_gaps': [{'id': 'gap'}]}})
    projection = action._research_context('topic', 'en', 1)['context_projection']
    assert projection['survey_omitted_chars'] == 0
    assert projection['gap_omitted_count'] == 0
    assert projection['gap_source_range'] == {'start': 0, 'end': 1}


def test_unbound_preview_still_declares_omitted_directional_material(monkeypatch):
    from lib.research import action, persistence
    monkeypatch.setattr(persistence, 'load_research_artifacts', lambda *a, **k:
        {'survey_md': 's' * 20000, 'open_gaps': {'open_gaps': [{'id': str(n)} for n in range(30)]}})
    result = action._research_context('topic', 'en', 1)
    assert len(result['survey_markdown']) == 16000
    assert len(result['gap_map']) == 20
    assert result['context_projection']['survey_omitted_chars'] == 4000
    assert result['context_projection']['gap_omitted_count'] == 10


def test_bound_complete_evidence_admission_scales_with_selected_model(monkeypatch):
    import json
    from lib.research import action, context, records
    original = {'accepted': [{'id': 'idea'}], 'survey_md': 's' * 200000 + 'FULL_SOURCE_TAIL',
                'open_gaps': {'open_gaps': [{'id': str(n)} for n in range(30)]}}
    monkeypatch.setattr(records, 'load_idea_source', lambda *a, **k: original)
    messages = action._messages({'action': 'analyze', 'direction': 'topic', 'lang': 'en', 'user_id': 12},
        {'selected_idea_source_task_id': 'source', 'selected_idea_id': 'idea'}, [])
    payload = json.loads(messages[1]['content'])['research_artifacts']
    assert payload['survey_markdown'].endswith('FULL_SOURCE_TAIL')
    assert payload['gap_map'][-1]['id'] == '29'
    monkeypatch.setattr('lib.token_counter.count_text', lambda *a, **k: 50000)
    monkeypatch.setattr(context, 'action_window', lambda model: 128000 if model == 'large' else 32000)
    assert context.admit_messages(messages, model='large') == 50000
    with pytest.raises(ValueError, match='exact sources were not truncated'):
        context.admit_messages(messages, model='small')
    assert json.loads(messages[1]['content'])['research_artifacts'] == payload


def test_oversized_bound_source_uses_owned_artifact_without_mutating_original(monkeypatch):
    import json
    from lib.research import context
    from lib.tool_result_artifacts import ToolResultArtifactRepository
    source = {'source_task_id': 'original', 'survey_markdown': 'evidence ' * 10000}
    messages = [{'role': 'system', 'content': 'system'}, {'role': 'user', 'content':
        json.dumps({'workspace': {'hypothesis': 'fixed'}, 'research_artifacts': source})}]
    before = json.loads(messages[1]['content'])
    monkeypatch.setattr(context, 'action_window', lambda model: 32000)
    monkeypatch.setattr('lib.token_counter.count_text', lambda text, **k: 50000 if 'evidence evidence' in text else 1000)
    stored = []
    def persist(self, *, content, user_id, media_type):
        stored.append((content, user_id))
        assert media_type == 'application/json'
        return {'artifactRef': 'a' * 64}
    monkeypatch.setattr(ToolResultArtifactRepository, 'put', persist)
    tools = [{'type': 'function', 'function': {'name': 'read_tool_artifact'}}]
    prepared = context.prepare_action_messages(messages, model='test', tools=tools, user_id=12)
    descriptor = json.loads(prepared[1]['content'])['research_artifacts']
    assert descriptor['artifact_ref'] == 'a' * 64
    assert descriptor['source_task_id'] == 'original'
    assert json.loads(stored[0][0]) == source and stored[0][1] == 12
    assert json.loads(prepared[1]['content'])['workspace'] == before['workspace']
    assert json.loads(messages[1]['content']) == before


def test_source_continuation_requires_read_tool_and_retention_ack(monkeypatch):
    import json
    from lib.research import context
    from lib.tool_result_artifacts import ToolResultArtifactRepository
    monkeypatch.setattr(context, 'action_window', lambda model: 32000)
    monkeypatch.setattr('lib.token_counter.count_text', lambda text, **k: 50000 if 'evidence evidence' in text else 1000)
    messages = [{'role': 'system', 'content': 'system'}, {'role': 'user', 'content': json.dumps(
        {'research_artifacts': {'source_task_id': 'source', 'survey_markdown': 'evidence ' * 10000}})}]
    with pytest.raises(context.ContextCapacityExceeded):
        context.prepare_action_messages(messages, model='test', tools=[], user_id=12)
    monkeypatch.setattr(ToolResultArtifactRepository, 'put', lambda *a, **k: {})
    with pytest.raises(ValueError, match='retention'):
        context.prepare_action_messages(messages, model='test', tools=[{'function': {'name': 'read_tool_artifact'}}], user_id=12)


def test_oversized_workspace_is_not_hidden_by_source_continuation(monkeypatch):
    import json
    from lib.research import context
    from lib.tool_result_artifacts import ToolResultArtifactRepository
    monkeypatch.setattr(context, 'action_window', lambda model: 32000)
    monkeypatch.setattr('lib.token_counter.count_text', lambda *a, **k: 50000)
    monkeypatch.setattr(ToolResultArtifactRepository, 'put', lambda *a, **k: pytest.fail('unusable source persisted'))
    messages = [{'role': 'system', 'content': 'system'}, {'role': 'user', 'content': json.dumps(
        {'workspace': {'source_files': 'oversized exact manuscript'},
         'research_artifacts': {'source_task_id': 'original', 'survey_markdown': 'source'}})}]
    with pytest.raises(context.ContextCapacityExceeded):
        context.prepare_action_messages(messages, model='test', tools=[{'function': {'name': 'read_tool_artifact'}}], user_id=12)
