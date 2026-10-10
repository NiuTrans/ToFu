"""Real local command and durable scientific settlement with a controlled model.

Provider frames contain synthetic measurements, explicitly not a research result.
The guarded loop, tool dispatch, journals, workspace CAS and terminal persistence
remain real. A conflicting model claim must not replace the command observation.
"""
import json
import shlex
import sys
import uuid
import pytest
from tests.research.test_research_workspace_evidence_guard import storage as storage
from tests.support.research_observations import observation_payload

pytestmark = pytest.mark.unit


@pytest.mark.parametrize('bind_selection',[False,True,'paged','restored','long','history','history_expired','live_retry','storage_restart'])
def test_local_command_reaches_durable_research_settlement(storage, monkeypatch, bind_selection, record_property, tmp_path):
    import lib.research.action as action
    from lib.research.action_runtime import create_action_task, _research_action_runtime
    from lib.research.workspace import empty_workspace, save_workspace, load_workspace
    from lib.research.model_accounting import ModelCalls
    from lib.research.action_observations import ActionObservations
    from lib.research.action_terminal import load_action_terminal
    from lib.llm.stream_result import ProviderStreamResult, ProviderStreamState
    import weakref
    admission_references = []; live_admission_counts = []; live_admission_bytes = []
    original_admit = ModelCalls.admit_model_call
    def measured_admit(repository, **kwargs):
        intent = original_admit(repository, **kwargs)
        admission_references.append((weakref.ref(repository), len(json.dumps(intent,
            ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode())))
        return intent
    monkeypatch.setattr(ModelCalls, 'admit_model_call', measured_admit)
    supervisor, backend = storage
    monkeypatch.setattr('lib.storage.get_storage_client', lambda **kwargs: supervisor.client)
    effect_log = tmp_path / 'executed-commands.txt'
    retry_failures = []
    if bind_selection in ('live_retry','storage_restart'):
        from lib.research.model_accounting import INTENT_NAMESPACE
        from lib.storage.errors import StorageError
        original_command = supervisor.client.command
        def command_with_busy_frontier(operation, payload, command_id):
            if (bind_selection == 'storage_restart' and payload.get('namespace') == INTENT_NAMESPACE
                    and payload['value']['ordinal'] == 8 and not retry_failures):
                retry_failures.append(8)
                from lib.research.execution_owner import ActionExecutionOwners, NAMESPACE as OWNER_NAMESPACE
                owners = ActionExecutionOwners.open(supervisor.client, user_id=1, task_id=task_id)
                owner_before = owners._get(OWNER_NAMESPACE, owners.prefix, 'executionOwner')
                usage_before = ModelCalls.open(supervisor.client, user_id=1, task_id=task_id).load_model_usage(7)
                assert effect_log.read_text().splitlines() == ['progress ' + str(n) for n in range(1, 8)]
                supervisor.stop(); supervisor.start()
                reopened = ActionExecutionOwners.open(supervisor.client, user_id=1, task_id=task_id)
                assert reopened._get(OWNER_NAMESPACE, reopened.prefix, 'executionOwner') == owner_before
                assert ModelCalls.open(supervisor.client, user_id=1, task_id=task_id).load_model_usage(7) == usage_before
                raise StorageError('database_unavailable', 'storage restarted before model admission', retryable=True)
            if (payload.get('namespace') == INTENT_NAMESPACE
                    and bind_selection == 'live_retry'
                    and payload['value']['ordinal'] in {8,12}
                    and retry_failures.count(payload['value']['ordinal']) <
                        (5 if payload['value']['ordinal'] == 8 else 2)):
                retry_failures.append(payload['value']['ordinal'])
                raise StorageError('database_busy', 'controlled unpaid frontier interruption', retryable=True)
            return original_command(operation, payload, command_id)
        monkeypatch.setattr(supervisor.client, 'command', command_with_busy_frontier)
    if not bind_selection:
        monkeypatch.setattr(action, '_research_context', lambda *args: {})
    if bind_selection in ('paged', 'restored', 'history', 'history_expired'):
        monkeypatch.setattr('lib.tool_result_artifacts.get_storage_client', lambda **kwargs: supervisor.client)
    if bind_selection == 'restored':
        monkeypatch.setattr('lib.research.context.restore_source_reference', lambda *a, **k: True)
    direction = 'Local scientific pipeline ' + uuid.uuid4().hex
    workspace = empty_workspace(direction)
    workspace['hypothesis'] = 'Controlled negative observation must override model optimism.'
    workspace['protocol'].update(dataset='synthetic fixture', primary_metric='accuracy', baseline='reference',
        random_seeds=[1,2,3], decision_rule={'direction':'higher','minimum_effect':0,'uncertainty_multiplier':2})
    if bind_selection:
        from lib.research import records
        monkeypatch.setattr(records,'_storage',lambda **kwargs:supervisor.client)
        records.persist_record({'task_id':'original-selection','user_id':1,'direction':direction,
            'lang':'en','status':'done','result':{'accepted':[{'id':'idea-1'}],
                'survey_md': ('Original bound source evidence\n' + ''.join(f'unique token evidence {n}\n' for n in range(14000))
                              if bind_selection in ('paged', 'restored') else 'Original bound source evidence')}})
        workspace.update(selected_idea_id='idea-1',selected_idea_source_task_id='original-selection')
    saved = save_workspace(direction, 'en', workspace, expected_revision=0, user_id=1)
    observed = observation_payload(saved['protocol'], 'synthetic-observation', 'refuted')
    command = shlex.quote(sys.executable) + ' -c ' + shlex.quote('print(' + repr(json.dumps(observed)) + ')')
    if bind_selection in ('live_retry','storage_restart'):
        command = shlex.quote(sys.executable) + ' -c ' + shlex.quote(
            'from pathlib import Path; Path(' + repr(str(effect_log)) + ').open("a").write("observation\\n"); '
            'print(' + repr(json.dumps(observed)) + ')')
    calls = []
    history_reads = []
    history_descriptors = []
    wire_chars = []
    history_mode = bind_selection in ('history', 'history_expired')
    if history_mode:
        monkeypatch.setattr('lib.research.context.action_window', lambda _: 16000)
        monkeypatch.setattr('lib.token_counter.count_text', lambda text, **kwargs: len(text) // 4)
    if bind_selection == 'history_expired':
        import time
        from lib.tool_result_artifacts import ToolResultArtifactRepository, DEFAULT_TTL_MS
        class ExpiredFirstHistory(ToolResultArtifactRepository):
            first_expired = False
            def put(self, **kwargs):
                if not type(self).first_expired and kwargs['content'].startswith('{"messages":'):
                    type(self).first_expired = True
                    kwargs['now_ms'] = int(time.time() * 1000) - 2 * DEFAULT_TTL_MS
                return super().put(**kwargs)
        monkeypatch.setattr('lib.tool_result_artifacts.ToolResultArtifactRepository', ExpiredFirstHistory)
    warmup_rounds = 15 if bind_selection in ('long', 'history', 'history_expired', 'live_retry', 'storage_restart') else 0
    def provider(messages, **kwargs):
        retained = [size for reference,size in admission_references if reference() is not None]
        live_admission_counts.append(len(retained)); live_admission_bytes.append(sum(retained))
        if bind_selection:
            if bind_selection not in ('paged', 'restored') or calls:
                assert 'Original bound source evidence' in json.dumps(messages)
            assert 'original-selection' in json.dumps(messages)
        if bind_selection == 'restored' and calls:
            assert messages[0]['content'].count('Previous continuation cursors were not renewed.') == 1
        calls.append(messages)
        if history_mode:
            wire_chars.append(len(json.dumps({'messages':messages,'tools':kwargs.get('tools') or []},
                ensure_ascii=False,separators=(',',':'))))
            from lib.research.history_context import KEY
            descriptors = [json.loads(row['content'])[KEY] for row in messages
                           if row.get('role') == 'system' and KEY in str(row.get('content'))]
            if descriptors:
                history_descriptors.extend(descriptors)
            if descriptors and not history_reads:
                history_reads.append(descriptors[0]['artifact_ref'])
                message = {'role':'assistant','content':'','tool_calls':[{'id':'read-history','type':'function',
                    'function':{'name':'read_tool_artifact','arguments':json.dumps({
                        'artifact_ref':history_reads[0],'cursor':'0','limit':2048})}}]}
                return ProviderStreamResult(message=message, compatibility_finish_reason='tool_calls',
                    provider_finish_reason='tool_calls', state=ProviderStreamState.PROVIDER_FINISHED,
                    saw_done=True, saw_finish_reason=True, usage=None)
        effective_round = len(calls) - len(history_reads)
        if effective_round <= warmup_rounds:
            progress_command = shlex.quote(sys.executable) + ' -c ' + shlex.quote('print(' + repr('progress '+str(effective_round)) + (" + 'x'*4000" if history_mode else '') + ')')
            if bind_selection in ('live_retry','storage_restart'):
                progress_command = shlex.quote(sys.executable) + ' -c ' + shlex.quote(
                    'from pathlib import Path; Path(' + repr(str(effect_log)) + ').open("a").write(' +
                    repr('progress ' + str(effective_round) + '\n') + '); print(' +
                    repr('progress ' + str(effective_round)) + ')')
            message = {'role':'assistant','content':'','tool_calls':[{'id':'progress-'+str(len(calls)),'type':'function',
                'function':{'name':'run_command','arguments':json.dumps({'command':progress_command})}}]}
            finish = 'tool_calls'
        elif bind_selection in ('paged', 'restored') and len(calls) == 1:
            descriptor = json.loads(messages[1]['content'])['research_artifacts']
            assert descriptor['delivery'] == 'read_tool_artifact'
            message = {'role':'assistant','content':'','tool_calls':[{'id':'read-source','type':'function',
                'function':{'name':'read_tool_artifact','arguments':json.dumps({
                    'artifact_ref':descriptor['artifact_ref'],'cursor':'0','limit':1024})}}]}
            finish = 'tool_calls'
        elif effective_round == warmup_rounds + (2 if bind_selection in ('paged', 'restored') else 1):
            message = {'role':'assistant','content':'','tool_calls':[{'id':'measure','type':'function',
                'function':{'name':'run_command','arguments':json.dumps({'command':command})}}]}
            finish = 'tool_calls'
        elif effective_round == warmup_rounds + (3 if bind_selection in ('paged', 'restored') else 2):
            assert any(row.get('role') == 'tool' for row in messages)
            message = {'role':'assistant','content':json.dumps({'summary':'Controlled final proposal.',
                'run':{'id':'controlled-run','status':'passed','scientific_outcome':'supported'}})}
            finish = 'stop'
        else: pytest.fail('unexpected additional model invocation')
        return ProviderStreamResult(message=message, compatibility_finish_reason=finish,
            provider_finish_reason=finish, state=ProviderStreamState.PROVIDER_FINISHED,
            saw_done=True, saw_finish_reason=True, usage=None)
    monkeypatch.setattr('lib.llm_dispatch.api.dispatch_stream', provider)
    task_id = 'research_action_' + uuid.uuid4().hex[:16]
    task = create_action_task(task_id, user_id=1, fields={'direction':direction,'lang':'en',
        'action':'experiment','expected_revision':saved['revision'],'confirm_external_writes':True,
        'model':'controlled-model','tool_rounds':[]})
    try:
        action.run_research_action(task)
        assert task['status'] == 'done', task.get('error')
        assert len(calls) == len(history_reads) + warmup_rounds + (3 if bind_selection in ('paged', 'restored') else 2)
        record_property('peak_live_model_admission_repositories', max(live_admission_counts))
        record_property('peak_live_model_admission_json_bytes', max(live_admission_bytes))
        assert max(live_admission_counts) == 1
        run = load_workspace(direction, 'en', user_id=1)['runs'][0]
        assert run['execution_status'] == 'completed'
        assert run['scientific_outcome'] == 'refuted'
        command_round = next(row for row in task['tool_rounds'] if row['toolName'] == 'code_exec')
        assert str(command_round['results'][0]['exitCode']) == '0'
        journal = ActionObservations.open(supervisor.client, user_id=1, task_id=task_id)
        assert journal.page(limit=1)['observations'][0]['receipt']['status'] == 'done'
        model_calls = ModelCalls.open(supervisor.client, user_id=1, task_id=task_id)
        assert model_calls.load_model_usage(len(calls))['usage_snapshot']['unmetered_calls'] == len(calls)
        terminal = load_action_terminal(task_id, user_id=1)
        assert terminal['found'] and terminal['terminal']['status'] == 'done'
        assert terminal['terminal']['committed_workspace_revision'] == saved['revision'] + 1
        latest_usage = model_calls.load_model_usage(len(calls))['usage_snapshot']
        assert task['result']['usage_snapshot'] == latest_usage
        assert terminal['terminal']['usage_snapshot'] == latest_usage
        if bind_selection in ('live_retry','storage_restart'):
            assert retry_failures == ([8] if bind_selection == 'storage_restart' else [8] * 5 + [12] * 2)
            assert effect_log.read_text().splitlines() == ['progress ' + str(n) for n in range(1, 16)] + ['observation']
            assert len([event for event in task['events'] if event.get('detail', '').startswith(
                'Continuing from the saved step')]) == (1 if bind_selection == 'storage_restart' else 2)
        if bind_selection in ('long', 'history', 'history_expired', 'live_retry', 'storage_restart'):
            from lib.research.loop_checkpoint import LoopCheckpoints
            checkpoints = LoopCheckpoints.open(supervisor.client, user_id=1, task_id=task_id)
            materials = [checkpoints.load_checkpoint(n) for n in range(1, len(calls))]
            assert all(material is not None for material in materials)
            assert set(materials[-1]['chassis']['claimed_tool_call_ids']) == {
                row['toolCallId'] for row in task['tool_rounds']}
            payload_bytes = sum(len(json.dumps(material, ensure_ascii=False).encode()) for material in materials)
            record_property('completed_round_checkpoint_payload_bytes', payload_bytes)
            if bind_selection == 'live_retry':
                record_property('recovered_checkpoint_json_bytes', [
                    len(json.dumps(materials[ordinal - 2], ensure_ascii=False,
                        sort_keys=True, separators=(',', ':')).encode()) for ordinal in sorted(set(retry_failures))])
            record_property('completed_model_dispatches', len(calls))
            assert len(task['tool_rounds']) == 16 + len(history_reads)
            if history_mode:
                assert history_reads and history_descriptors
                read_round = next(row for row in task['tool_rounds'] if row['toolCallId'] == 'read-history')
                assert 'progress 1' in read_round['toolContent']
                if bind_selection == 'history_expired':
                    assert read_round.get('sourceReferenceRestored') is True
                assert sum(row['toolName'] == 'code_exec' for row in task['tool_rounds']) == 16
                record_property('history_reference_count', len({row['artifact_ref'] for row in history_descriptors}))
                record_property('largest_provider_input_chars', max(wire_chars))
                assert max(wire_chars) // 4 <= 16000 - 2000 - 2048
            assert latest_usage['forced_final_reason'] == ''
            assert latest_usage['agent_dispatch_budget'] == latest_usage['agent_token_budget'] == 0
    finally:
        _research_action_runtime.remove_owned(task_id, user_id=1)
