"""Complete scientific observations survive lossy presentation and persistence."""
import hashlib
import json
from copy import deepcopy
import pytest
from lib.research.action import capture_research_result, _tool_receipts, apply_action_result
from lib.research.decisions import manuscript_audit
from lib.research.program import normalize_run_evidence
from lib.research.workspace import empty_workspace
from tests.support.research_observations import observation_payload

pytestmark=pytest.mark.unit
PROTOCOL={'baseline':'reference','dataset':'held-out','primary_metric':'accuracy','random_seeds':[1,2,3]}


def captured_row(payload):
    payload=dict(log='x'*20000, **payload)
    text=json.dumps(payload,ensure_ascii=False)
    row={'toolName':'run_command','status':'done','toolCallId':'call'}
    capture_research_result(row,text)
    # The shared model-visible budget replaces output with an artifact descriptor.
    row['toolContent']='presentation-only summary'
    row['toolResultArtifactRef']='retained:owner-scoped'
    return row,text


def test_measurements_survive_presentation_changes_and_normalization():
    row,text=captured_row(observation_payload(PROTOCOL,'metrics'))
    receipt=_tool_receipts([row])[0]
    assert receipt['result_digest']==hashlib.sha256(text.encode()).hexdigest()
    assert receipt['artifact_ref']=='retained:owner-scoped'
    assert len(receipt['result_excerpt'])==4000
    measurement=json.loads(receipt['observation_excerpt'])
    assert 'log' not in measurement
    run={'id':'metrics','execution_status':'completed','validity':'current',
         'artifact_ref':'metrics','scientific_outcome':'supported','tool_receipts':[receipt]}
    normalized=normalize_run_evidence([run])[0]
    assert normalized['tool_receipts'][0]['observation_excerpt']==receipt['observation_excerpt']
    assert manuscript_audit([run],[{'status':'supported','validity':'current'}],protocol=PROTOCOL)['passed']


def test_large_scientific_outcome_is_observed_without_trusting_model_proposal():
    row,_=captured_row(observation_payload(PROTOCOL,'metrics','refuted'))
    result=apply_action_result(action='experiment',workspace=empty_workspace('Scope'),
        task_id='task',bindings=[],payload={'run':{'id':'metrics','scientific_outcome':'supported'}},
        receipts=_tool_receipts([row]))
    assert result['runs'][0]['scientific_outcome']=='refuted'


def test_error_beyond_display_prefix_cannot_become_success():
    row,_=captured_row({'ok':False,'error':'remote experiment failed'})
    assert _tool_receipts([row])[0]['status']=='error'


def test_oversized_scientific_payload_is_never_persisted_as_partial_json():
    payload=observation_payload(PROTOCOL,'metrics')
    payload['scientific_observations']['unknown_large_field']='x'*5000
    row,_=captured_row(payload)
    assert 'observation_excerpt' not in _tool_receipts([row])[0]


def test_missing_measurement_preserves_legacy_receipt_shape():
    row,_=captured_row({'status':'completed'})
    receipt=_tool_receipts([row])[0]
    assert 'observation_excerpt' not in receipt
    assert 'observation_excerpt' not in normalize_run_evidence([{'id':'r','tool_receipts':[receipt]}])[0]['tool_receipts'][0]

from tests.research.test_research_workspace_evidence_guard import storage as storage


def test_captured_measurements_survive_archive_and_cannot_be_rewritten(storage):
    from tests.research.test_research_history import draft_with_runs, save, DIRECTION
    from lib.research.workspace import load_workspace, load_history_page, save_workspace
    draft=draft_with_runs(24)
    for run in draft['runs']:
        reference=run['artifact_ref']
        row,_=captured_row(observation_payload(draft['protocol'],reference))
        # Retained artifact reference is supplied by the owner-scoped tool pipeline.
        row['toolResultArtifactRef']=reference
        run['tool_receipts']=_tool_receipts([row])
    saved=save(draft)
    restored=load_workspace(DIRECTION,'en',user_id=1)
    assert all(value==0 for value in restored['history']['audit_failures'].values())
    page=load_history_page(DIRECTION,'en',user_id=1)
    assert page['runs']
    assert all('observation_excerpt' in r['tool_receipts'][0] for r in page['runs'])
    changed=deepcopy(restored)
    changed['runs'][0]['tool_receipts'][0]['observation_excerpt']='{}'
    with pytest.raises(ValueError):
        save_workspace(DIRECTION,'en',changed,user_id=1,expected_revision=saved['revision'])


@pytest.mark.parametrize("refusal", ["none", "basis", "intent", "original"])
def test_live_execution_callback_captures_before_result_budget(monkeypatch, refusal):
    import threading
    from types import SimpleNamespace
    import lib.research.action as action
    import lib.research.action_runtime as runtime
    content=json.dumps(dict(log='x'*20000,**observation_payload(PROTOCOL,'metrics')))
    monkeypatch.setattr(action,'_messages',lambda *args:[])
    monkeypatch.setattr(action,'make_paper_exec_shim',lambda **kwargs:None)
    executed = []
    def execute(*args, **kwargs):
        executed.append(True)
        return content, None, None, None, None
    monkeypatch.setattr(action,'execute_paper_tool',execute)
    def freeze(task, workspace):
        if refusal == 'basis':
            from lib.storage.errors import StorageError
            raise StorageError('database_unavailable', 'injected original capture refusal')
        def intent(**kwargs):
            if refusal == 'intent':
                from lib.storage.errors import StorageError
                raise StorageError('database_unavailable', 'injected original capture refusal')
        return SimpleNamespace(record_intent=intent, capture=lambda **kwargs: journal(None, None, None, kwargs['content']))
    monkeypatch.setattr('lib.research.action_observations.freeze_action_observation_basis', freeze)
    monkeypatch.setattr(runtime,'append_action_event',lambda *args:None)
    captured = []
    def journal(task, workspace, row, result):
        if refusal == 'original':
            from lib.storage.errors import StorageError
            raise StorageError('database_unavailable', 'injected original capture refusal')
        captured.append(result)
    class Budget:
        def __init__(self,**kwargs):pass
        def append(self,*args,**kwargs):
            assert captured == [content]
            kwargs['round_entry']['toolContent']='lossy presentation'
            return 'lossy presentation'
        def finish_round(self,*args):pass
    monkeypatch.setattr(action,'PaperToolResultBudgetV2',Budget)
    def loop(**kwargs):
        kwargs['execute_tool'](1,{'id':'call','function':{'name':'run_command','arguments':'{}'}})
        kwargs['on_round_result'](1,{'content':'{}'},None,None)
        return SimpleNamespace(completed=True)
    monkeypatch.setattr(action,'run_guarded_paper_agent_loop',loop)
    task={'user_id':1,'model':'test','task_id':'task','action':'experiment','direction':'Scope',
          'abort_event':threading.Event(),'tool_rounds':[]}
    epoch=SimpleNamespace(result_envelope='v2',wire_schemas=[],contract_documents_by_name={})
    if refusal != 'none':
        from lib.storage.errors import StorageError
        with pytest.raises(StorageError, match='injected original capture refusal'):
            action._run_agent(task,empty_workspace('Scope'),epoch,[])
        assert captured == []
        assert bool(executed) == (refusal == 'original')
        return
    _,receipts=action._run_agent(task,empty_workspace('Scope'),epoch,[])
    assert receipts[0]['result_digest']==hashlib.sha256(content.encode()).hexdigest()
    assert 'scientific_observations' in json.loads(receipts[0]['observation_excerpt'])


@pytest.mark.parametrize('count', [64, 65])
def test_receipt_extraction_and_normalization_never_hide_tail_failure(count):
    from lib.research.program import MAX_TOOL_RECEIPTS
    rows = [{'toolName': 'run_command', 'status': 'done',
             'toolCallId': str(index), 'toolContent': '{}'} for index in range(count)]
    rows[-1]['status'] = 'error'
    if count <= MAX_TOOL_RECEIPTS:
        receipts = _tool_receipts(rows)
        assert len(receipts) == count and receipts[-1]['status'] == 'error'
        normalized = normalize_run_evidence([{'id': 'complete', 'tool_receipts': receipts}])
        assert len(normalized[0]['tool_receipts']) == count
        assert normalized[0]['tool_receipts'][-1]['status'] == 'error'
    else:
        with pytest.raises(ValueError, match='research_tool_receipts_overflow'):
            _tool_receipts(rows)
        receipts = [{'status': 'done'} for _ in rows]
        receipts[-1]['status'] = 'error'
        with pytest.raises(ValueError, match='research_tool_receipts_overflow'):
            normalize_run_evidence([{'id': 'oversized', 'tool_receipts': receipts}])


def test_live_tool_uses_acknowledged_basis_even_if_workspace_changes(monkeypatch):
    import threading
    from types import SimpleNamespace
    import lib.research.action as action
    import lib.research.action_runtime as runtime
    from lib.research.action_observations import ActionObservations
    from tests.research.test_research_action_observations import MemoryRecords
    client = MemoryRecords()
    workspace = empty_workspace('Scope'); workspace['hypothesis'] = 'Original declared hypothesis'
    basis = {'hypothesis': workspace['hypothesis'], 'protocol': deepcopy(workspace['protocol'])}
    monkeypatch.setattr('lib.storage.get_storage_client', lambda **kwargs: client)
    monkeypatch.setattr(action, '_messages', lambda *args: [])
    monkeypatch.setattr(action, 'make_paper_exec_shim', lambda **kwargs: None)
    monkeypatch.setattr(runtime, 'append_action_event', lambda *args: None)
    def execute(*args, **kwargs):
        saved = ActionObservations.open(client, user_id=1, task_id='frozen-live-tool')
        assert saved.load_basis() == basis  # Must exist before the effect boundary.
        assert saved.load_intent(1)['call_id'] == 'call'
        workspace['hypothesis'] = 'Mutated after tool admission'
        return 'observed output', None, None, None, None
    monkeypatch.setattr(action, 'execute_paper_tool', execute)
    class Budget:
        def __init__(self, **kwargs): pass
        def append(self, *args, **kwargs): return 'observed output'
        def finish_round(self, *args): pass
    monkeypatch.setattr(action, 'PaperToolResultBudgetV2', Budget)
    def loop(**kwargs):
        kwargs['execute_tool'](1, {'id': 'call', 'function': {'name': 'run_command', 'arguments': '{}'}})
        kwargs['on_round_result'](1, {'content': '{}'}, None, None)
        return SimpleNamespace(completed=True)
    monkeypatch.setattr(action, 'run_guarded_paper_agent_loop', loop)
    task = {'user_id': 1, 'model': 'test', 'task_id': 'frozen-live-tool', 'action': 'experiment',
            'direction': 'Scope', 'abort_event': threading.Event(), 'tool_rounds': []}
    epoch = SimpleNamespace(result_envelope='v2', wire_schemas=[], contract_documents_by_name={})
    action._run_agent(task, workspace, epoch, [])
    restored = ActionObservations.open(client, user_id=1, task_id='frozen-live-tool')
    assert restored.load_basis() == basis
    assert restored.read(1)[1] == 'observed output'


def test_oversized_early_science_cannot_disappear_behind_later_valid_support():
    from lib.research.science_reduction import reduce_scientific_receipts
    early = observation_payload(PROTOCOL, 'metrics', 'refuted')
    early['scientific_observations']['unknown_large_field'] = 'x' * 5000
    row, _ = captured_row(early)
    first = _tool_receipts([row])[0]
    assert first['observation_incomplete'] is True
    first = normalize_run_evidence([{'id': 'r', 'tool_receipts': [first]}])[0]['tool_receipts'][0]
    late, _ = captured_row(observation_payload(PROTOCOL, 'metrics', 'supported'))
    summary = reduce_scientific_receipts(iter([first, _tool_receipts([late])[0]]),
        expected_outcome='supported', protocol=PROTOCOL, evidence_refs=['metrics'])
    assert not summary['outcome_consistent'] and not any(summary['checks'].values())
    assert summary['scientific_outcome'] == 'inconclusive'


def test_large_log_preserves_explicit_outcome_without_measurement_table():
    row, _ = captured_row({'scientific_outcome': 'refuted'})
    receipt = _tool_receipts([row])[0]
    assert json.loads(receipt['observation_excerpt']) == {'scientific_outcome': 'refuted'}


@pytest.mark.parametrize('marker', [False, 1, 'true', None])
def test_incomplete_observation_marker_rejects_noncanonical_values(marker):
    with pytest.raises(ValueError, match='incomplete observation marker'):
        normalize_run_evidence([{'id': 'r', 'tool_receipts': [{'observation_incomplete': marker}]}])


def test_invalid_captured_outcome_does_not_crash_result_application():
    row, _ = captured_row({'scientific_outcome': ['not-a-label']})
    result = apply_action_result(action='experiment', workspace=empty_workspace('Invalid report'),
        task_id='invalid-report', bindings=[], payload={'run': {'id':'r'}}, receipts=_tool_receipts([row]))
    assert result['runs'][0]['scientific_outcome'] == 'inconclusive'
