"""Canonical execution provenance, selected quote durability and honest coverage."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
from types import SimpleNamespace

import pytest

from tests.memory.test_working_facts import (
    facts as facts, artifact as artifact, storage as storage, turn_service,
    PRINCIPAL, query, owner,
)
from tests.tasks_pkg.test_native_gateway import task as gateway_task
from lib.agent_core.native_process import ApprovalRequest, ToolRequest
from lib.memory.working_facts import repository, service
from lib.tasks_pkg.native_gateway import NativeGateway
from lib.tasks_pkg.working_tool_sources import (
    begin_tool_source, settle_tool_source, available_tool_sources, tool_quote_resolver,
)
from lib.tools.result_value import CapturedToolResult, ResultPart, TrustedResultText

pytestmark = pytest.mark.unit


def task_for(facts):
    return gateway_task() | {'convId': facts['conv_id'], '_principalContext': PRINCIPAL.to_payload()}


def settle(task, text='Budget: 100', *, call_id='observation', coverage='unknown', status='done', artifact_ref=''):
    begin_tool_source(task, 'fixture_observe', call_id)
    canonical = CapturedToolResult((ResultPart('text', text),), source_coverage=coverage,
        artifact_ref=artifact_ref, artifact_owner=1 if artifact_ref else 0)
    row = {'toolCallId': call_id, 'toolContent': text, 'status': status}
    task['toolRounds'].append(row)
    settle_tool_source(task, 'fixture_observe', call_id, row, canonical)
    return canonical, row


def save(facts, task, source, *, quote='Budget: 100', evidence=None):
    return service.record_fact(facts['conv_id'], {'action': 'record', 'revision': query(facts)['revision'],
        'kind': 'observation', 'text': quote, 'sources': [source | {'quote': quote}]}, principal=PRINCIPAL,
        task_id=task['id'], tool_sources=evidence or tool_quote_resolver(task))['facts'][0]


@pytest.mark.parametrize('mode', ['legacy', 'v2', 'spilled', 'posthook'])
def test_native_gateway_canonical_results_can_be_quoted_without_fabricated_origin(facts, storage, monkeypatch, mode):
    from lib.tool_results import tool_result_artifacts
    from lib.tasks_pkg.executor import _build_simple_meta, _finalize_tool_round
    from lib.tasks_pkg.handlers.working_facts import handle_working_facts
    from lib.tools.working_facts import WORKING_FACT_CONTRACTS
    monkeypatch.setattr(tool_result_artifacts, 'get_storage_client', lambda write=False: storage.client)
    task = task_for(facts); task['config']['tools']['resultEnvelope'] = 'legacy' if mode == 'legacy' else 'v2'
    text = ('hidden original evidence\n' * 6000 if mode == 'spilled' else '') + 'Budget: 100'
    if mode == 'posthook':
        text = 'private secret\n' + text
        monkeypatch.setattr('lib.tasks_pkg.tool_dispatch._pipeline.run_post_hooks',
            lambda name, args, value, task: value.replace('private secret', '[redacted]'))
    calls = []
    def observe(task, tc, name, call_id, args, rn, row, *rest, **kwargs):
        calls.append(call_id)
        _finalize_tool_round(task, rn, row, [_build_simple_meta(name, text, source='fixture')], status='done')
        return call_id, text, False
    definitions = [{'type': 'function', 'function': {'name': 'fixture_observe', 'description': 'Observe fixture',
        'parameters': {'type': 'object', 'properties': {}}}}, *[c.provider_schema() for c in WORKING_FACT_CONTRACTS]]
    task['_executable_tool_catalog'] = definitions
    task['_tool_env'] = SimpleNamespace(resolve=lambda name: observe if name == 'fixture_observe' else handle_working_facts,
        write_names={'record_working_fact'}, idempotent_names={'fixture_observe', 'read_working_facts'}, confirmation_names=set())
    async def run(executor):
        gateway = NativeGateway(principal=PRINCIPAL, task=task, executor=executor, configuration={'autoApply': True},
            tool_list=definitions, permissions={'fixture_observe': 'read', 'read_working_facts': 'read', 'record_working_fact': 'write'},
            project_path=None, project_enabled=False, model='fixture', round_source=lambda: 0,
            decision_policy=None, pending_calls=4, pending_bytes=65536)
        async def execute(name, args, identifier):
            return await gateway.execute(ToolRequest(PRINCIPAL, task['id'], identifier+'-request', name, args, identifier, model_round=0))
        try:
            visible = await execute('fixture_observe', {}, 'first')
            assert calls
            catalog = json.loads(await execute('read_working_facts', {'include_tool_sources': True}, 'sources'))
            assert len(catalog['toolSources']) == 1, (visible, catalog)
            source = catalog['toolSources'][0]
            assert source['toolCallId'] == calls[0] and source['temporality'] == 'historical_execution'
            if mode == 'spilled':
                assert len(visible) < len(text)
            if mode == 'posthook':
                assert 'private secret' not in visible
                with pytest.raises(ValueError, match='quote'):
                    save(facts, task, source, quote='private secret')
            arguments = {'revision': 0, 'kind': 'observation', 'text': 'Budget: 100', 'sources': [source | {'quote': 'Budget: 100'}]}
            approval = ApprovalRequest(PRINCIPAL, task['id'], 'save-approval', 'save', 'record_working_fact',
                'epoch', json.dumps(arguments).encode(), b'null', asyncio.Event(), 2)
            assert await gateway.decide(approval) == 'auto_write_mode'
            recorded = json.loads(await execute('record_working_fact', arguments, 'save'))
            assert 'facts' in recorded, recorded
            assert recorded['facts'][0]['status'] == 'observed', recorded
            assert recorded['facts'][0]['reasons'] == ['historical_tool_observation']
        finally:
            gateway.close()
    with ThreadPoolExecutor(max_workers=1) as executor:
        asyncio.run(run(executor))


def test_quote_seal_survives_compaction_restart_and_retains_only_selected_evidence(facts, storage):
    task = task_for(facts); settle(task, 'private unselected detail\nBudget: 100')
    source = available_tool_sources(task)['toolSources'][0]
    fact = save(facts, task, source)
    task['toolRounds'].clear(); task.pop('_working_tool_sources')
    storage.stop(); storage.start()
    assert query(facts)['facts'][0]['status'] == 'observed'
    assert save(facts, task, source)['status'] == 'observed'
    document, _ = repository.FactRepository(facts['conv_id'], PRINCIPAL).read()
    assert 'private unselected detail' not in json.dumps(document)
    with pytest.raises(repository.FactConflict): save(facts, task, source, quote='private unselected detail')
    owner(facts, fact, 'correct', text='Owner supplied budget')
    assert next(row for row in query(facts)['facts'] if row['id'] == fact['id'])['status'] == 'owner_asserted'


@pytest.mark.parametrize('case', ['no_start', 'failed', 'cancelled', 'terminal', 'scope', 'cache', 'partial', 'preview'])
def test_unexecuted_failed_changed_or_incomplete_sources_are_not_issued(facts, case):
    task = task_for(facts)
    if case != 'no_start': begin_tool_source(task, 'fixture_observe', 'call')
    if case == 'cancelled': task['aborted'] = True
    if case == 'terminal': task['status'] = 'done'
    if case == 'scope': task['convId'] += '-changed'
    canonical = CapturedToolResult((ResultPart('text', 'Budget: 100'),),
        retention_coverage='partial' if case == 'partial' else 'complete')
    row = {'status': 'error' if case == 'failed' else 'done', 'toolContent': 'Preview' if case == 'preview' else 'Budget: 100'}
    if case == 'cache': row['cacheSource'] = 'cache'
    settle_tool_source(task, 'fixture_observe', 'call', row, canonical)
    assert not task.get('_working_tool_sources')
    assert not task.get('_working_tool_source_starts')


@pytest.mark.parametrize('case', ['quote', 'source_metadata', 'compacted', 'foreign_task', 'fake_json'])
def test_new_quote_requires_current_task_host_receipt_and_exact_original_carrier(facts, case):
    task = task_for(facts); _, row = settle(task)
    source = available_tool_sources(task)['toolSources'][0]
    if case == 'quote':
        with pytest.raises(ValueError, match='quote'): save(facts, task, source, quote='Fabricated budget')
        return
    if case == 'source_metadata': source['sourceCoverage'] = 'complete'
    if case == 'compacted': row['toolContent'] = 'Summary: Budget: 100'
    if case == 'foreign_task': task['id'] = 'another-task'
    if case == 'fake_json': task['_working_tool_sources'][source['id']] = dict(source)
    with pytest.raises(repository.FactConflict): save(facts, task, source)
    assert query(facts)['facts'] == []


def test_source_metadata_pages_eviction_and_terminal_release(facts, monkeypatch):
    from lib.tasks_pkg import working_tool_sources as sources
    from lib.tasks_pkg.manager._persist import _release_heavy_task_state
    monkeypatch.setattr(sources, 'tool_result_cache_capacity', lambda: 52)
    task = task_for(facts)
    for index in range(60): settle(task, call_id=str(index))
    assert len(task['_working_tool_sources']) == 52
    first = available_tool_sources(task); second = available_tool_sources(task, first['toolSourcesNextCursor'])
    assert len(first['toolSources']) == 50 and len(second['toolSources']) == 2
    assert not second['toolSourcesNextCursor']
    assert first['toolSources'][0]['toolCallId'] == '59'
    task['status'] = 'done'; _release_heavy_task_state(task)
    assert not task.get('_working_tool_sources') and not task.get('_working_tool_source_starts')


@pytest.mark.parametrize('shape', ['long_unicode', 'split_parts', 'duplicate_child_ids'])
def test_retained_quote_crosses_pages_but_never_independent_segments(facts, storage, monkeypatch, shape):
    import json
    from uuid import uuid4
    from lib.tool_results.tool_result_artifacts import ToolResultArtifactRepository
    from lib.tool_results.tool_result_query_contract import MEDIA_TYPE
    monkeypatch.setattr('lib.tool_results.tool_result_artifacts.get_storage_client', lambda write=False: storage.client)
    repo = ToolResultArtifactRepository()
    quote = '记录' * 1000 if shape == 'long_unicode' else 'Budget: 100'
    def put(text, media='text/plain'):
        return repo.put(user_id=1, content=text, media_type=media)['artifactRef']
    if shape == 'long_unicode':
        parts, children = [{'kind': 'artifact', 'artifactRef': put('x' * 64000 + quote + 'tail')}], []
    elif shape == 'split_parts':
        parts, children = [{'kind': 'text', 'text': value} for value in ['Budget: ', '100']], []
    else:
        parts, children = [], [{'name': 'same', 'callId': 'same', 'artifactRef': put(value)} for value in ['Budget: ', '100']]
    ref = put(json.dumps(dict(resultId=str(uuid4()), executionStatus='ok', sourceCoverage='complete',
        retentionCoverage='complete', parts=parts, children=children)), MEDIA_TYPE)
    task = task_for(facts); settle(task, artifact_ref=ref)
    source = available_tool_sources(task)['toolSources'][0]
    if shape == 'long_unicode':
        assert save(facts, task, source, quote=quote)['status'] == 'observed'
    else:
        with pytest.raises(repository.FactConflict):
            save(facts, task, source, quote=quote)


def test_retention_expiry_preserves_sealed_history_but_cannot_introduce_new_quote(facts, storage, monkeypatch):
    from lib.tool_results.tool_result_artifacts import ToolResultArtifactRepository
    monkeypatch.setattr('lib.tool_results.tool_result_artifacts.get_storage_client', lambda write=False: storage.client)
    repo = ToolResultArtifactRepository()
    saved = repo.put(user_id=1, content='Budget: 100\nStatus: pending')
    task = task_for(facts); settle(task, artifact_ref=saved['artifactRef'])
    source = available_tool_sources(task)['toolSources'][0]
    fact = save(facts, task, source)
    storage.client.maintenance('tool_result_artifact.prune', {'user_id': 1, 'now_ms': saved['expiresAtMs'] + 1, 'limit': 100}, deadline=5)
    assert next(item for item in query(facts)['facts'] if item['id'] == fact['id'])['status'] == 'observed'
    with pytest.raises(repository.FactConflict):
        save(facts, task, source, quote='Status: pending')


@pytest.mark.parametrize('caller_kind', ['direct', 'program'])
def test_actual_file_observation_keeps_history_while_live_basis_invalidates_dependents(facts, storage, monkeypatch, tmp_path, caller_kind):
    import re
    from lib.tasks_pkg.handlers.project import _handle_project_tool
    from lib.tasks_pkg.orchestrator._programmatic import reconcile_programmatic_items
    from lib.tasks_pkg.working_file_sources import file_witness_resolver
    from lib.tools.project import READ_FILES_TOOL
    from lib.memory.working_facts import consumers
    monkeypatch.setattr(consumers, 'get_storage_client', lambda write=False: storage.client)
    task = task_for(facts); task['config']['projectPath'] = str(tmp_path)
    path = tmp_path / 'basis.txt'; path.write_text('Budget: 100\n')
    task['_executable_tool_catalog'] = [READ_FILES_TOOL]
    task['_tool_env'] = SimpleNamespace(resolve=lambda name: _handle_project_tool,
        write_names=set(), idempotent_names={'read_files'}, confirmation_names=set())
    caller = None
    if caller_kind == 'program':
        parent = {'_responses_items': [{'type': 'program', 'call_id': 'provider-program',
            'code': 'await tools.read_files({path:"basis.txt",bind_sources:true})'}]}
        reconcile_programmatic_items(task, parent, llm_round=0)
        caller = {'type': 'program', 'caller_id': task['programRuns'][0]['callId']}
    async def run(executor):
        gateway = NativeGateway(principal=PRINCIPAL, task=task, executor=executor,
            configuration={'autoApply': True}, tool_list=[READ_FILES_TOOL], permissions={'read_files': 'read'},
            project_path=str(tmp_path), project_enabled=True, model='fixture', round_source=lambda: 0,
            decision_policy=None, pending_calls=4, pending_bytes=65536)
        try:
            return await gateway.execute(ToolRequest(PRINCIPAL, task['id'], 'read-execute', 'read_files',
                {'path': 'basis.txt', 'bind_sources': True}, 'actual-read', caller=caller, model_round=0))
        finally:
            gateway.close()
    with ThreadPoolExecutor(max_workers=1) as executor:
        output = asyncio.run(run(executor))
    descriptors = available_tool_sources(task)['toolSources']
    assert len(descriptors) == 1, output
    assert descriptors[0]['toolName'] == 'read_files'
    historical = save(facts, task, descriptors[0])
    file_source = json.loads(re.search(r'\[File source: (\{.*\})\]', output)[1])
    live = service.record_fact(facts['conv_id'], {'action': 'record', 'revision': query(facts)['revision'],
        'kind': 'observation', 'text': 'Budget: 100', 'sources': [file_source | {'quote': 'Budget: 100'}]},
        principal=PRINCIPAL, task_id=task['id'], file_sources=file_witness_resolver(task))['facts'][0]
    derived = service.record_fact(facts['conv_id'], {'action': 'record', 'revision': query(facts)['revision'],
        'kind': 'derived', 'text': 'Plan from observed execution and current file', 'sources': [
            {'kind': 'fact', 'id': row['id'], 'version': row['revision']} for row in [historical, live]]},
        principal=PRINCIPAL, task_id=task['id'])['facts'][0]
    for identifier, fact in [('affected', derived), ('history-only', historical)]:
        refs = [{'id': fact['id'], 'revision': fact['revision']}]
        consumers.prepare(facts['conv_id'], principal=PRINCIPAL, task_id=identifier, facts=refs)
        consumers.confirm(facts['conv_id'], principal=PRINCIPAL, task_id=identifier, facts=refs)
    path.write_text('Budget: 200\n')
    states = {row['id']: row['status'] for row in query(facts)['facts']}
    assert states[historical['id']] == 'observed'
    assert states[live['id']] == states[derived['id']] == 'stale'
    impact = consumers.query(facts['conv_id'], principal=PRINCIPAL)
    rows = {row['taskId']: row for row in impact['consumers']}
    assert rows['affected']['requiresRevalidation'] and not rows['history-only']['requiresRevalidation']
