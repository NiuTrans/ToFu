"""Authorized physical reads, sealed quotes, freshness and task impact."""
import json
import re

import pytest

from tests.memory.test_working_facts import (
    facts as facts, artifact as artifact, storage as storage, turn_service,
    PRINCIPAL, query, record, owner, dependency,
)
from tests.tasks_pkg.test_native_gateway import task as gateway_task
from tests.memory.test_working_facts_context import quiet_notes as quiet_notes
from lib.memory.working_facts import service, repository, file_sources
from lib.project_mod.tools import execute_tool
from lib.tasks_pkg.working_file_sources import FileReadCapture, file_witness_resolver

pytestmark = pytest.mark.unit


def read_source(facts, tmp_path, *, publish=True, path='basis.txt', data='Budget: 100\nRegion: east\n'):
    (tmp_path / path).write_text(data)
    task = gateway_task() | {'convId': facts['conv_id'], '_principalContext': PRINCIPAL.to_payload()}
    task['config']['projectPath'] = str(tmp_path)
    capture = FileReadCapture(task, 'read-basis')
    result = execute_tool('read_files', {'reads': [{'path': path, 'start_line': 1, 'end_line': 1}],
        'bind_sources': True}, str(tmp_path), conv_id=task['convId'], task_id=task['id'],
        task=task, file_source_capture=capture)
    match = re.search(r'\[File source: (\{.*\})\]', result)
    assert match, result
    assert 'Region: east' not in result  # Display range is unchanged; digest covers all bytes.
    if publish:
        capture.publish({'status': 'done'})
    return task, capture, json.loads(match[1]) | {'quote': 'Budget: 100'}


def save(facts, task, source, *, text=None):
    return service.record_fact(facts['conv_id'], {'action': 'record', 'revision': query(facts)['revision'],
        'text': text or source['quote'], 'kind': 'observation', 'sources': [source]},
        principal=PRINCIPAL, task_id=task['id'], file_sources=file_witness_resolver(task))['facts'][0]


def test_real_read_exact_quote_restart_and_transitive_file_change(facts, tmp_path, storage, monkeypatch):
    task, _, source = read_source(facts, tmp_path)
    fact = save(facts, task, source)
    child = record(facts, text='Use this budget', kind='derived', evidence=[dependency(fact)])['facts'][0]
    independent = record(facts)['facts'][0]
    before = query(facts)
    storage.stop(); storage.start()
    task.pop('_working_file_sources')
    assert query(facts) == before
    # Existing exact seals can be reused without a task-local witness after restart.
    assert save(facts, task, source)['status'] == 'observed'
    with pytest.raises(repository.FactConflict):
        save(facts, task, source | {'quote': 'Region: east'})
    (tmp_path / 'basis.txt').write_text('Budget: 200\nRegion: east\n')
    result = {row['id']: row for row in query(facts)['facts']}
    assert result[fact['id']]['status'] == result[child['id']]['status'] == 'stale'
    assert result[independent['id']]['status'] == 'observed'


@pytest.mark.parametrize('case', ['pending', 'error', 'cancelled', 'scope', 'forged', 'path', 'version', 'root_revoked'])
def test_unsettled_forged_or_foreign_read_cannot_introduce_a_source(facts, tmp_path, monkeypatch, case):
    task, capture, source = read_source(facts, tmp_path, publish=False)
    if case == 'error': capture.publish({'status': 'error'})
    if case == 'cancelled':
        task['aborted'] = True
        with pytest.raises(TimeoutError): capture.publish({'status': 'done'})
        task['aborted'] = False
    if case == 'scope':
        task['convId'] += '-other'
        with pytest.raises(PermissionError): capture.publish({'status': 'done'})
        task['convId'] = facts['conv_id']
    if case == 'forged': task['_working_file_sources'] = {(source['id'], source['version']): dict(source)}
    if case in {'path', 'version', 'root_revoked'}:
        capture.publish({'status': 'done'})
        if case == 'path': source['path'] = str(tmp_path / 'secret.txt')
        elif case == 'version': source['version'] = 'a' * 64
        else: task['_workspaceRootPolicy'] = {'primaryPath': '', 'roots': {}}
    def denied(*a, **k): pytest.fail('Unissued source caused filesystem I/O')
    monkeypatch.setattr(file_sources, 'read_project_snapshot', denied)
    with pytest.raises(repository.FactConflict): save(facts, task, source)
    assert query(facts)['facts'] == []


def test_quote_mismatch_and_changed_read_version_are_rejected(facts, tmp_path):
    task, _, source = read_source(facts, tmp_path)
    with pytest.raises(ValueError, match='quote'):
        save(facts, task, source | {'quote': 'Invented budget'})
    (tmp_path / 'basis.txt').write_text('Budget: 100\nA changed second line\n')
    with pytest.raises(repository.FactConflict): save(facts, task, source)
    assert query(facts)['facts'] == []


@pytest.mark.parametrize('replacement', ['symlink', 'root', 'missing'])
def test_replaced_root_and_aliases_never_supply_current_evidence(facts, tmp_path, replacement):
    root = tmp_path / 'root'; root.mkdir()
    task, _, source = read_source(facts, root)
    save(facts, task, source)
    target = root / 'basis.txt'
    if replacement == 'root':
        root.rename(tmp_path / 'old-root'); root.mkdir(); target.write_text('Budget: 100\nRegion: east\n')
    else:
        target.unlink()
        if replacement == 'symlink':
            secret = tmp_path / 'secret.txt'; secret.write_text('Budget: 100\nRegion: east\n'); target.symlink_to(secret)
    assert query(facts)['facts'][0]['status'] == 'unavailable'


def test_seals_are_bounded_metadata_pruned_on_correction_and_inactive_sources_are_not_read(facts, tmp_path, monkeypatch):
    task, _, source = read_source(facts, tmp_path, data='Budget: 100\n' + 'large body\n' * 50000)
    fact = save(facts, task, source)
    document, _ = repository.FactRepository(facts['conv_id'], PRINCIPAL).read()
    assert len(json.dumps(document).encode()) < 4096
    assert len(document['_fileSources']) == 1
    owner(facts, fact, 'retract')
    def denied(*a, **k): pytest.fail('Inactive fact reread a file')
    monkeypatch.setattr(file_sources, 'read_project_snapshot', denied)
    assert query(facts)['facts'][0]['status'] == 'withdrawn'
    owner(facts, fact, 'correct', text='Owner supplied budget')
    document, _ = repository.FactRepository(facts['conv_id'], PRINCIPAL).read()
    assert document['_fileSources'] == {}


def test_live_query_hashes_each_file_once_even_with_multiple_sealed_quotes(facts, tmp_path, monkeypatch):
    task, _, source = read_source(facts, tmp_path)
    save(facts, task, source)
    save(facts, task, source | {'quote': 'Region: east'})
    reads = []
    original = file_sources.read_project_snapshot
    def tracked(*a, **k): reads.append(a); return original(*a, **k)
    monkeypatch.setattr(file_sources, 'read_project_snapshot', tracked)
    assert all(row['status'] == 'observed' for row in query(facts)['facts'])
    assert len(reads) == 1


def test_metadata_receipts_evict_and_release_at_task_terminal(facts, tmp_path, monkeypatch):
    from lib.tools import resource_policy
    from lib.tasks_pkg.manager._persist import _release_heavy_task_state
    monkeypatch.setattr(resource_policy, 'tool_result_cache_capacity', lambda: 2)
    task, _, source = read_source(facts, tmp_path)
    from lib.artifacts.file_snapshot import read_project_snapshot
    capture = FileReadCapture(task, 'more-reads')
    for i in range(4):
        path = tmp_path / f'{i}.txt'; path.write_text(f'evidence {i}')
        capture.bind(read_project_snapshot(str(path), [str(tmp_path)]), roots=[str(tmp_path)])
    assert len(capture.pending) == 2
    capture.publish({'status': 'done'})
    assert len(task['_working_file_sources']) == 2
    assert file_witness_resolver(task)(source) is None
    task['status'] = 'done'
    _release_heavy_task_state(task)
    assert not task.get('_working_file_sources')


def test_physical_file_change_invalidates_only_dependent_task_consumers(facts, tmp_path, monkeypatch):
    from lib.memory.working_facts import consumers
    monkeypatch.setattr(consumers, 'get_storage_client', repository.get_storage_client)
    task, _, source = read_source(facts, tmp_path)
    fact = save(facts, task, source)
    child = record(facts, text='Plan', kind='derived', evidence=[dependency(fact)])['facts'][0]
    independent = record(facts)['facts'][0]
    for name, basis in [('dependent', child), ('independent', independent)]:
        refs = [{'id': basis['id'], 'revision': basis['revision']}]
        consumers.prepare(facts['conv_id'], principal=PRINCIPAL, task_id=name, facts=refs)
        consumers.confirm(facts['conv_id'], principal=PRINCIPAL, task_id=name, facts=refs)
    (tmp_path / 'basis.txt').write_text('Budget: 200\n')
    impact = {row['taskId']: row for row in consumers.query(facts['conv_id'], principal=PRINCIPAL)['consumers']}
    assert impact['dependent']['requiresRevalidation']
    assert not impact['independent']['requiresRevalidation']


def test_actual_native_gateway_issues_and_records_source_after_settlement(facts, tmp_path):
    import asyncio
    from concurrent.futures import ThreadPoolExecutor
    from types import SimpleNamespace
    from lib.agent_core.native_process import ApprovalRequest, ToolRequest
    from lib.tasks_pkg.native_gateway import NativeGateway
    from lib.tasks_pkg.handlers.project import _handle_project_tool
    from lib.tasks_pkg.handlers.working_facts import handle_working_facts
    from lib.tools.working_facts import WORKING_FACT_CONTRACTS
    from lib.tools.project import READ_FILES_TOOL
    task = gateway_task() | {'convId': facts['conv_id'], '_principalContext': PRINCIPAL.to_payload()}
    task['config']['projectPath'] = str(tmp_path)
    (tmp_path / 'basis.txt').write_text('Budget: 100\n')
    definitions = [READ_FILES_TOOL, *[contract.provider_schema() for contract in WORKING_FACT_CONTRACTS]]
    task['_executable_tool_catalog'] = definitions
    task['_tool_env'] = SimpleNamespace(resolve=lambda name: _handle_project_tool if name == 'read_files' else handle_working_facts,
        write_names={'record_working_fact'}, idempotent_names={'read_files', 'read_working_facts'}, confirmation_names=set())
    async def run(executor):
        gateway = NativeGateway(principal=PRINCIPAL, task=task, executor=executor,
            configuration={'autoApply': True}, tool_list=definitions,
            permissions={'read_files': 'read', 'read_working_facts': 'read', 'record_working_fact': 'write'},
            project_path=str(tmp_path), project_enabled=True, model='fixture', round_source=lambda: 0,
            decision_policy=None, pending_calls=4, pending_bytes=65536)
        try:
            result = await gateway.execute(ToolRequest(PRINCIPAL, task['id'], 'read-execute',
                'read_files', {'path': 'basis.txt', 'bind_sources': True}, 'actual-read', model_round=0))
            match = re.search(r'\[File source: (\{.*\})\]', result)
            assert match, result
            source = json.loads(match[1]) | {'quote': 'Budget: 100'}
            witness = file_witness_resolver(task)(source)
            assert witness.call_id == task['toolRounds'][0]['toolCallId']
            assert task['toolRounds'][0]['status'] == 'done'
            arguments = {'revision': 0, 'text': 'Budget: 100', 'kind': 'observation', 'sources': [source]}
            approval = ApprovalRequest(PRINCIPAL, task['id'], 'save-approval', 'save',
                'record_working_fact', 'epoch', json.dumps(arguments).encode(), b'null', asyncio.Event(), 2)
            assert await gateway.decide(approval) == 'auto_write_mode'
            saved = await gateway.execute(ToolRequest(PRINCIPAL, task['id'], 'save-execute',
                'record_working_fact', arguments, 'save', model_round=0))
            assert json.loads(saved)['facts'][0]['status'] == 'observed'
        finally:
            gateway.close()
    with ThreadPoolExecutor(max_workers=1) as executor:
        asyncio.run(run(executor))


def test_context_refreshes_file_freshness_without_a_fact_revision_change(facts, tmp_path, quiet_notes):
    from lib.tasks_pkg.context_composer.working_facts import refresh_working_facts
    from tests.memory.test_working_facts_context import latest_text
    task, _, source = read_source(facts, tmp_path)
    save(facts, task, source)
    messages = [{'role': 'user', 'content': 'Continue using the recorded budget'}]
    refresh_working_facts(messages, task=task, model='fixture')
    revision = query(facts)['revision']
    reused = refresh_working_facts(messages, task=task, model='fixture')
    assert not reused.manifest[0]['appended']
    (tmp_path / 'basis.txt').write_text('Budget: 150\n')
    changed = refresh_working_facts(messages, task=task, model='fixture')
    assert query(facts)['revision'] == revision
    assert changed.manifest[0]['appended'] and 'stale' in latest_text(messages)
