"""Backend-neutral storage.v1 contract exercised through the real process."""

from __future__ import annotations

import json
import os
from pathlib import Path
import threading
import time

import pytest

from lib.storage import (
    StorageClient, StorageError, StorageRuntime, StorageSupervisor,
)
from lib.storage.errors import http_status_for_storage_error
from lib.storage_sidecar.preflight import ProjectLease


pytestmark = pytest.mark.unit
_BACKENDS = ['sqlite']
if os.environ.get('TOFU_STORAGE_TEST_POSTGRES') == '1':
    _BACKENDS.append('postgres')


@pytest.fixture(params=_BACKENDS)
def storage(request, tmp_path: Path, monkeypatch):
    monkeypatch.setenv('TOFU_STORAGE_SQLITE_READ_POOL', '2')
    monkeypatch.setenv('TOFU_STORAGE_PG_READ_POOL', '2')
    monkeypatch.setenv('TOFU_STORAGE_PG_WRITE_POOL', '1')
    supervisor = StorageSupervisor(
        project_root=tmp_path, backend=request.param, startup_timeout=60)
    supervisor.start()
    try:
        yield supervisor
    finally:
        supervisor.stop()


def test_health_preflight_and_project_local_files(storage, tmp_path):
    health = storage.client.health()
    assert health['ready'] is True
    assert health['backend'] in _BACKENDS
    assert health['protocol'] == 'storage.v1'
    assert health['preflight']['atomic_replace'] is True
    assert health['preflight']['file_lock'] is True
    authority = ('tofu.db' if health['backend'] == 'sqlite' else 'pgdata')
    assert (tmp_path / 'data' / authority).exists()
    assert not list(tmp_path.glob('.storage-preflight-*'))
    metrics = storage.client.metrics()
    rpc = metrics['rpc']
    assert rpc['capacity'] == 256
    assert 1 <= rpc['active'] <= rpc['capacity']
    assert metrics['process']['rss_bytes'] > 0
    assert metrics['process']['open_fds_or_handles'] > 0
    assert metrics['process']['threads'] >= 1


def test_project_lease_stamp_distinguishes_running_from_clean_stop(tmp_path):
    data_dir = tmp_path / 'data'
    data_dir.mkdir()
    lease = ProjectLease(data_dir)
    lease.acquire()
    lease_path = data_dir / '.storage-sidecar-lease.json'
    running = json.loads(lease_path.read_text(encoding='utf-8'))
    assert running['status'] == 'running'
    assert running['pid'] == os.getpid()

    lease.release()

    stopped = json.loads(lease_path.read_text(encoding='utf-8'))
    assert stopped['lease_id'] == running['lease_id']
    assert stopped['status'] == 'stopped'
    assert stopped['stopped_unix_ms'] >= stopped['started_unix_ms']


def test_sqlite_boot_path_does_not_run_an_unbounded_full_integrity_scan():
    import inspect
    from lib.storage_sidecar.adapters.sqlite import SQLiteBackend

    startup_source = inspect.getsource(SQLiteBackend.start)
    maintenance_source = inspect.getsource(SQLiteBackend.integrity_check)
    assert "writer_connection.execute('PRAGMA integrity_check')" not in startup_source
    assert 'PRAGMA integrity_check' in maintenance_source


def test_process_service_reports_ready_and_releases_owner(tmp_path, monkeypatch):
    from lib.storage.service import (
        install_runtime_for_test,
        start_storage,
        stop_storage,
        storage_status,
    )

    monkeypatch.setenv('TOFU_STORAGE_SQLITE_READ_POOL', '1')
    runtime = StorageRuntime(StorageSupervisor(
        project_root=tmp_path, backend='sqlite', startup_timeout=15))
    install_runtime_for_test(runtime)
    try:
        assert storage_status()['state'] == 'stopped'
        start_storage()
        ready = storage_status()
        assert ready['ready'] is True
        assert ready['state'] == 'ready'
        assert ready['backend'] == 'sqlite'
        assert isinstance(ready['pid'], int)

        stop_storage(timeout=5.0)

        assert storage_status()['state'] == 'not_started'
        assert runtime.status()['state'] == 'stopped'
    finally:
        install_runtime_for_test(None)


def test_command_receipt_replay_and_conflict(storage):
    payload = {'namespace': 'contract', 'key': 'once', 'value': {'amount': 7}}
    first = storage.client.command('record.put', payload, 'same-command')
    replay = storage.client.command('record.put', payload, 'same-command')
    assert replay == first
    assert storage.client.query(
        'record.get', {'namespace': 'contract', 'key': 'once'})['version'] == 1

    with pytest.raises(StorageError) as raised:
        storage.client.command(
            'record.put', {**payload, 'value': {'amount': 8}}, 'same-command')
    assert raised.value.code == 'database_conflict'
    assert raised.value.retryable is False
    assert http_status_for_storage_error(raised.value) == 409


def test_natural_event_key_deduplicates_without_receipt(storage):
    payload = {'task_id': 'task-1', 'sequence': 3, 'event': {'kind': 'delta'}}
    assert storage.client.command('event.append', payload, None, priority='event')['inserted']
    assert not storage.client.command(
        'event.append', payload, None, priority='event')['inserted']
    assert storage.client.query('event.list', {'task_id': 'task-1'}) == [{
        'sequence': 3,
        'event': {'kind': 'delta'},
        'created_at_ms': storage.client.query(
            'event.list', {'task_id': 'task-1'})[0]['created_at_ms'],
    }]
    with pytest.raises(StorageError) as raised:
        storage.client.command('event.append', {
            **payload, 'event': {'kind': 'conflicting'},
        }, None, priority='event')
    assert raised.value.code == 'database_conflict'


def test_event_batch_is_atomic_and_naturally_deduplicated(storage):
    events = [{
        'task_id': f'batch-task-{index % 2}', 'sequence': index // 2,
        'event': {'kind': 'delta', 'index': index},
    } for index in range(100)]
    first = storage.client.command(
        'event.append_batch', {'events': events}, None, priority='event')
    replay = storage.client.command(
        'event.append_batch', {'events': events}, None, priority='event')
    assert first['inserted'] == 100 and first['deduplicated'] == 0
    assert replay['inserted'] == 0 and replay['deduplicated'] == 100
    assert len(replay['results']) == 100


def test_rate_limit_bucket_admission_is_atomic(storage):
    workers = 16
    limit = 5
    barrier = threading.Barrier(workers)

    def hit(index: int):
        barrier.wait(timeout=10)
        command_id = f'rate-hit-{index}'
        return storage.client.command(
            'rate_limit.record_and_check',
            {
                'endpoint': '/contract',
                'client_key': '198.51.100.8',
                'event_id': command_id,
                'limit': limit,
                'per_seconds': 60,
            },
            command_id,
        )

    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(hit, range(workers)))
    assert sum(1 for item in results if item['allowed']) == limit
    assert all(item['count'] <= limit for item in results)


def test_orchestration_aggregate_semantics(storage):
    assert storage.client.command(
        'orchestration.run.create',
        {
            'run_id': 'run-contract', 'definition': {'nodes': []},
            'input': 'go', 'orch_id': 'flow-1', 'name': 'Contract',
            'created_by': 'tester',
        },
        'orch-create-contract',
    ) == {'created': True}
    created = storage.client.query(
        'orchestration.run.get', {'run_id': 'run-contract'})
    assert created['status'] == 'pending'
    assert created['definition'] == {'nodes': []}

    event = {'type': 'flow_start', 'node_id': 'root'}
    projected = storage.client.command(
        'orchestration.event.project',
        {'run_id': 'run-contract', 'sequence': 0,
         'event': event, 'status': 'running'},
        None,
    )
    assert projected == {'projected': True, 'inserted': True}
    assert storage.client.command(
        'orchestration.event.project',
        {'run_id': 'run-contract', 'sequence': 0,
         'event': event, 'status': 'running'},
        None,
    ) == {'projected': True, 'inserted': False}
    with pytest.raises(StorageError) as conflict:
        storage.client.command(
            'orchestration.event.append',
            {'run_id': 'run-contract', 'sequence': 0,
             'event': {'type': 'different'}},
            None,
        )
    assert conflict.value.code == 'database_conflict'

    page = storage.client.query(
        'orchestration.event.page',
        {'run_id': 'run-contract', 'cursor': 0})
    assert page['events'] == [{**event, 'seq': 0}]
    assert page['caught_up'] is True
    assert storage.client.command(
        'orchestration.run.update_status',
        {'run_id': 'run-contract', 'status': 'done', 'final': 'ok'},
        'orch-finish-contract',
    )['changed']
    with pytest.raises(StorageError) as terminal:
        storage.client.command(
            'orchestration.event.project',
            {'run_id': 'run-contract', 'sequence': 1,
             'event': {'type': 'late'}, 'status': 'running'},
            None,
        )
    assert terminal.value.code == 'database_conflict'
    assert storage.client.query(
        'orchestration.event.page',
        {'run_id': 'run-contract', 'cursor': 0})['events'] == [
            {**event, 'seq': 0}]
    assert storage.client.command(
        'orchestration.run.delete', {'run_id': 'run-contract'},
        'orch-delete-contract')['deleted']
    assert storage.client.query(
        'orchestration.run.get', {'run_id': 'run-contract'}) is None


def test_swarm_checkpoint_aggregate_semantics(storage):
    client = storage.client
    assert client.command(
        'swarm.session.save', {
            'swarm_key': 'swarm-contract', 'conv_id': 'conv-1',
            'task_id': 'task-1', 'status': 'running',
            'specs': [{'id': 'a1'}], 'config': {'model': 'test'},
            'now_ms': 100,
        }, 'swarm-session-create') == {'saved': True}
    assert client.command(
        'swarm.session.save', {
            'swarm_key': 'swarm-contract', 'conv_id': 'conv-2',
            'task_id': 'task-2', 'status': 'running',
            'specs': [{'id': 'a1'}], 'config': {'model': 'test-2'},
            'now_ms': 200,
        }, 'swarm-session-update') == {'saved': True}
    client.command(
        'swarm.agent.save', {
            'swarm_key': 'swarm-contract', 'agent_id': 'a1',
            'role': 'coder', 'objective': 'resume safely',
            'status': 'completed', 'messages': [{'role': 'assistant'}],
            'result': {'final_answer': 'done'}, 'rounds_used': 1,
            'delivered': True, 'now_ms': 300,
        }, 'swarm-agent-create')
    client.command(
        'swarm.agent.save', {
            'swarm_key': 'swarm-contract', 'agent_id': 'a1',
            'role': 'coder', 'objective': 'resume safely',
            'status': 'completed', 'messages': [{'role': 'assistant', 'content': 'done'}],
            'result': {'final_answer': 'done'}, 'rounds_used': 2,
            'delivered': None, 'now_ms': 400,
        }, 'swarm-agent-update')
    detail = client.query(
        'swarm.session.get', {'swarm_key': 'swarm-contract'})
    assert detail['conv_id'] == 'conv-2'
    assert detail['created_at'] == 100
    assert detail['updated_at'] == 200
    assert detail['agents'][0]['delivered'] is True
    assert detail['agents'][0]['rounds_used'] == 2
    assert client.query('swarm.resumable.list', {}) == []

    client.command(
        'swarm.agent.save', {
            'swarm_key': 'swarm-contract', 'agent_id': 'a1',
            'role': 'coder', 'objective': 'resume safely',
            'status': 'running', 'messages': [], 'result': {},
            'rounds_used': 2, 'delivered': None, 'now_ms': 500,
        }, 'swarm-agent-running')
    resumable = client.query('swarm.resumable.list', {})
    assert [item['swarm_key'] for item in resumable] == ['swarm-contract']
    assert resumable[0]['agents'][0]['status'] == 'running'

    assert client.command(
        'swarm.session.delete', {'swarm_key': 'swarm-contract'},
        'swarm-session-delete') == {'deleted': True}
    assert client.query(
        'swarm.session.get', {'swarm_key': 'swarm-contract'}) is None


def test_research_artifact_aggregate_semantics(storage):
    client = storage.client
    client.command('paper.report.upsert', {
        'paper_hash': 'paper-contract', 'lang': 'en',
        'report': 'ordinary paper', 'model': 'm',
        'meta': {'direction': 'must not leak', 'kind': 'insight'},
        'created_at': 999,
    }, 'paper-report-contract')
    base = {
        'paper_hash': 'research-contract', 'model': 'm', 'created_at': 1000,
    }
    assert client.command('research.artifact.upsert', {
        **base, 'lang_key': 'survey:en', 'report': '# survey',
        'meta': {'kind': 'survey', 'direction': 'storage architecture',
                 'open_gaps': {'open_gaps': []}},
    }, 'research-survey-contract') == {'saved': True}
    client.command('research.artifact.upsert', {
        **base, 'lang_key': 'ideate:en', 'report': '# ideas',
        'meta': {'kind': 'ideate', 'direction': 'storage architecture',
                 'accepted': [{'id': 'a'}], 'rejected': [{'id': 'r'}],
                 'gate_reached': 'accepted'},
    }, 'research-ideate-contract')

    artifacts = client.query('research.artifacts.get', {
        'paper_hash': 'research-contract', 'lang': 'en',
    })
    assert [item['lang_key'] for item in artifacts] == [
        'ideate:en', 'survey:en']
    listed = client.query('research.directions.list', {'limit': 50})
    assert [item['direction'] for item in listed] == ['storage architecture']
    assert listed[0]['accepted'] == 1
    assert listed[0]['has_survey'] is True
    assert client.query('paper.report.get', {
        'paper_hash': 'paper-contract', 'lang': 'en',
    })['report'] == 'ordinary paper'


def test_paper_second_pass_merge_is_atomic_and_preserves_siblings(storage):
    client = storage.client
    client.command('paper.report.upsert', {
        'paper_hash': 'second-pass-contract', 'lang': 'en',
        'report': 'body', 'model': 'm', 'created_at': 1000,
        'meta': {
            'promptTokens': 100, 'completionTokens': 20,
            'costCny': 0.01, 'costUsd': 0.001,
        },
    }, 'second-pass-report')
    entries = {
        'insight': {
            'usage': {'prompt_tokens': 10, 'completion_tokens': 2},
            'costCny': 0.002, 'costUsd': 0.0002,
        },
        'checkpoints': {
            'usage': {'prompt_tokens': 5, 'completion_tokens': 1},
            'costCny': 0.001, 'costUsd': 0.0001,
        },
    }

    def merge(item):
        name, entry = item
        return client.command('paper.report.second_pass.merge', {
            'paper_hash': 'second-pass-contract', 'lang': 'en',
            'name': name, 'entry': entry,
        }, f'second-pass-{name}')

    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(merge, entries.items()))
    assert all(result['found'] for result in results)

    stored = client.query('paper.report.get', {
        'paper_hash': 'second-pass-contract', 'lang': 'en',
    })['meta']
    assert set(stored['secondPasses']) == {'insight', 'checkpoints'}
    assert stored['totalUsage']['prompt_tokens'] == 115
    assert stored['totalUsage']['completion_tokens'] == 23
    assert stored['totalCostCny'] == pytest.approx(0.013)
    assert stored['totalCostUsd'] == pytest.approx(0.0013)

    def accumulate(index):
        return client.command('paper.report.second_pass.accumulate', {
            'paper_hash': 'second-pass-contract', 'lang': 'en',
            'name': 'deepen',
            'usage': {'prompt_tokens': 5, 'completion_tokens': 2},
            'costCny': 0.0005, 'costUsd': 0.00005,
        }, f'second-pass-accumulate-{index}')

    with ThreadPoolExecutor(max_workers=2) as pool:
        accumulated = list(pool.map(accumulate, range(2)))
    assert all(result['found'] for result in accumulated)
    stored = client.query('paper.report.get', {
        'paper_hash': 'second-pass-contract', 'lang': 'en',
    })['meta']
    assert stored['secondPasses']['deepen']['calls'] == 2
    assert stored['secondPasses']['deepen']['usage']['prompt_tokens'] == 10
    assert stored['totalUsage']['prompt_tokens'] == 125
    assert stored['totalUsage']['completion_tokens'] == 27
    assert stored['totalCostCny'] == pytest.approx(0.014)
    assert stored['totalCostUsd'] == pytest.approx(0.0014)

    missing = client.command('paper.report.second_pass.merge', {
        'paper_hash': 'missing-report', 'lang': 'en',
        'name': 'insight', 'entry': {},
    }, 'second-pass-missing')
    assert missing == {'found': False, 'meta': None}


def test_paper_translation_semantics(storage):
    client = storage.client
    assert client.query('paper.translation.get', {
        'paper_hash': 'translation-contract', 'lang': 'review:neurips:zh',
    }) is None
    assert client.command('paper.translation.upsert', {
        'paper_hash': 'translation-contract', 'lang': 'review:neurips:zh',
        'text': '第一版', 'model': 'm1', 'created_at': 1000,
    }, 'translation-create') == {'saved': True}
    assert client.command('paper.translation.upsert', {
        'paper_hash': 'translation-contract', 'lang': 'review:neurips:zh',
        'text': '第二版', 'model': 'm2', 'created_at': 2000,
    }, 'translation-update') == {'saved': True}
    assert client.query('paper.translation.get', {
        'paper_hash': 'translation-contract', 'lang': 'review:neurips:zh',
    }) == {
        'paper_hash': 'translation-contract', 'lang': 'review:neurips:zh',
        'text': '第二版', 'model': 'm2', 'created_at': 2000,
    }


def test_paper_library_context_and_identity_semantics(storage):
    client = storage.client
    for index, title in enumerate(('Current paper', 'Prior one', 'Prior two')):
        assert client.command('paper.library.put', {
            'id': f'paper-{index}', 'user_id': 1, 'title': title,
            'arxiv_id': f'2608.0000{index}',
            'paper_hash': f'hash-{index}',
            'parsed_text': f'parsed-{index}',
            'created_at': 1000 + index, 'updated_at': 1000 + index,
        }, f'paper-library-{index}') == {'saved': True}

    assert client.query('paper.library.identity', {
        'paper_hash': 'hash-0',
    }) == {
        'title': 'Current paper', 'arxiv_id': '2608.00000',
        'parsed_text': 'parsed-0',
    }
    assert client.query('paper.library.recent', {
        'exclude_paper_hash': 'hash-0', 'limit': 40,
    }) == [
        {'title': 'Prior two', 'arxiv_id': '2608.00002'},
        {'title': 'Prior one', 'arxiv_id': '2608.00001'},
    ]


def test_paper_library_title_backfill_preserves_real_titles(storage):
    client = storage.client
    for index, title in enumerate(('', 'arXiv:2608.10001')):
        client.command('paper.library.put', {
            'id': f'paper-title-{index}', 'user_id': index + 1,
            'title': title, 'paper_hash': 'title-backfill-contract',
            'created_at': 1000 + index, 'updated_at': 1000 + index,
        }, f'paper-title-seed-{index}')
    assert client.command('paper.library.title.backfill', {
        'paper_hash': 'title-backfill-contract', 'title': 'Recovered title',
    }, 'paper-title-backfill') == {'title': 'Recovered title', 'updated': 2}
    assert client.query('paper.library.identity', {
        'paper_hash': 'title-backfill-contract',
    })['title'] == 'Recovered title'

    client.command('paper.library.put', {
        'id': 'paper-title-custom', 'user_id': 3,
        'title': 'My reading notes',
        'paper_hash': 'title-backfill-contract',
        'created_at': 4_000_000_000, 'updated_at': 4_000_000_000,
    }, 'paper-title-custom')
    assert client.command('paper.library.title.backfill', {
        'paper_hash': 'title-backfill-contract', 'title': 'Wrong replacement',
    }, 'paper-title-backfill-again') == {
        'title': 'My reading notes', 'updated': 0,
    }


def test_daily_cost_cache_semantics(storage):
    client = storage.client
    for day, cost in (('2026-08-12', 1.25), ('2026-08-13', 2.5)):
        assert client.command('daily_cost.upsert', {
            'user_id': 1, 'date': day, 'cost': cost,
            'conversations': {'conv-1': {'cost': cost, 'tokens': 10}},
            'computed_at': 1000,
        }, f'daily-cost-{day}') == {'saved': True}
    month = client.query('daily_cost.month', {
        'user_id': 1, 'year': 2026, 'month': 8,
    })
    assert [row['date'] for row in month] == ['2026-08-12', '2026-08-13']
    assert month[-1]['conversations']['conv-1']['cost'] == 2.5
    assert client.query('daily_cost.latest', {'user_id': 1})['date'] == '2026-08-13'
    assert client.query('daily_cost.persisted_dates', {
        'user_id': 1, 'dates': ['2026-08-11', '2026-08-13'],
    }) == {'dates': ['2026-08-13']}
    assert client.command('daily_cost.delete', {
        'user_id': 1, 'date': '2026-08-12',
    }, 'daily-cost-delete-one') == {'deleted': 1}
    assert client.command('daily_cost.delete', {
        'user_id': 1,
    }, 'daily-cost-delete-all') == {'deleted': 1}
    assert client.query('daily_cost.latest', {'user_id': 1}) is None


def test_paper_podcast_semantics(storage):
    client = storage.client
    key = {
        'paper_hash': 'podcast-contract', 'mode': 'short',
        'lang': 'zh', 'voice': 'alloy',
    }
    assert client.query('paper.podcast.get', key) is None
    assert client.command('paper.podcast.upsert', {
        **key, 'status': 'generating', 'script': {},
        'meta': {'task_id': 'pod-1'}, 'duration_sec': 0,
        'created_at': 1000, 'updated_at': 1000,
    }, 'podcast-generating') == {'saved': True}
    assert client.command('paper.podcast.mark_interrupted', {
        'updated_at': 2000,
    }, 'podcast-interrupt') == {'changed': 1}
    assert client.query('paper.podcast.get', key)['status'] == 'interrupted'
    assert client.command('paper.podcast.upsert', {
        **key, 'status': 'done',
        'script': {'segments': [{'text': 'hello'}]},
        'meta': {'source_kind': 'report_zh'}, 'file_path': 'paper.wav',
        'duration_sec': 3.5, 'model': 'writer', 'tts_model': 'voice',
        'created_at': 1000, 'updated_at': 3000,
    }, 'podcast-done') == {'saved': True}
    row = client.query('paper.podcast.get', key)
    assert row['status'] == 'done'
    assert row['script_json']['segments'][0]['text'] == 'hello'
    assert row['meta'] == {'source_kind': 'report_zh'}
    assert row['duration_sec'] == 3.5


def test_artifact_semantics(storage):
    client = storage.client
    base = {
        'conv_id': 'artifact-contract', 'task_id': 'task-a',
        'msg_id': 'message-a', 'source': 'write_file',
        'format': 'markdown', 'title': 'report.md',
        'source_ref': {'path': 'report.md'}, 'meta': {'words': 2},
    }
    first = client.command('artifact.create', {
        **base, 'artifact_id': 'artifact-v1', 'content': '# first\n',
        'created_at': 100,
    }, 'artifact-create-v1')
    assert first['created'] is True
    assert first['artifact']['version'] == 1
    assert first['artifact']['parent_id'] == ''
    assert first['artifact']['meta'] == {'words': 2}

    duplicate = client.command('artifact.create', {
        **base, 'artifact_id': 'artifact-duplicate', 'content': '# first\n',
        'created_at': 101,
    }, 'artifact-create-duplicate')
    assert duplicate == {'created': False, 'artifact': first['artifact']}

    second = client.command('artifact.create', {
        **base, 'artifact_id': 'artifact-v2', 'content': '# second\n',
        'created_at': 102,
    }, 'artifact-create-v2')
    assert second['artifact']['version'] == 2
    assert second['artifact']['parent_id'] == 'artifact-v1'
    full = client.query('artifact.get', {
        'artifact_id': 'artifact-v2', 'include_content': True,
    })
    assert full['content'] == '# second\n'
    assert [row['id'] for row in client.query(
        'artifact.versions', {'artifact_id': 'artifact-v2'})
    ] == ['artifact-v1', 'artifact-v2']

    assert client.command('artifact.pin', {
        'artifact_id': 'artifact-v1', 'pinned': True,
    }, 'artifact-pin-v1') == {'changed': True}
    library = client.query('artifact.library', {'limit': 20})
    assert [row['id'] for row in library[:2]] == [
        'artifact-v1', 'artifact-v2']
    assert client.command('artifact.delete', {
        'artifact_id': 'artifact-v2', 'deleted_at': 200,
    }, 'artifact-delete-v2') == {'deleted': True}
    assert client.query('artifact.get', {
        'artifact_id': 'artifact-v2', 'include_content': False,
    }) is None
    listed = client.query('artifact.list', {
        'conv_id': 'artifact-contract', 'include_deleted': True,
    })
    assert {row['id'] for row in listed} == {'artifact-v1', 'artifact-v2'}


def test_artifact_concurrent_dedupe_is_atomic(storage):
    from concurrent.futures import ThreadPoolExecutor

    barrier = threading.Barrier(8)

    def create(index):
        barrier.wait()
        return storage.client.command('artifact.create', {
            'artifact_id': f'artifact-race-{index}',
            'conv_id': 'artifact-race', 'source': 'inline_doc',
            'source_ref': {}, 'format': 'html', 'title': 'race.html',
            'content': '<h1>same</h1>', 'meta': {},
            'created_at': 100 + index,
        }, f'artifact-race-command-{index}')

    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(create, range(8)))
    assert sum(result['created'] for result in results) == 1
    assert len({result['artifact']['id'] for result in results}) == 1
    assert len(storage.client.query('artifact.list', {
        'conv_id': 'artifact-race', 'include_deleted': False,
    })) == 1


def test_tenant_user_semantics(storage):
    client = storage.client
    payload = {
        'user_id': 'tenant-user-1', 'email': 'owner@example.com',
        'password_hash': 'pbkdf2$salt$digest', 'display_name': 'Owner',
        'role': 'user', 'metadata': {'oidc_sub': 'subject-1'},
        'created_at': 100,
    }
    created = client.command(
        'tenant.user.create', payload, 'tenant-user-create-1')
    assert created == client.command(
        'tenant.user.create', payload, 'tenant-user-create-1')
    assert created['email'] == 'owner@example.com'
    assert created['metadata'] == {'oidc_sub': 'subject-1'}
    assert 'password_hash' not in created
    assert client.query('tenant.user.get', {
        'email': 'OWNER@example.com',
    })['id'] == 'tenant-user-1'

    with pytest.raises(StorageError) as duplicate:
        client.command('tenant.user.create', {
            **payload, 'user_id': 'tenant-user-2',
        }, 'tenant-user-create-duplicate')
    assert duplicate.value.code == 'database_conflict'

    auth = client.query(
        'tenant.user.authentication', {'email': 'owner@example.com'})
    assert auth['password_hash'] == 'pbkdf2$salt$digest'
    assert auth['user']['id'] == 'tenant-user-1'
    updated = client.command('tenant.user.set_role', {
        'user_id': 'tenant-user-1', 'role': 'admin',
    }, 'tenant-user-role-1')
    assert updated['role'] == 'admin'
    updated = client.command('tenant.user.set_status', {
        'user_id': 'tenant-user-1', 'status': 'suspended',
    }, 'tenant-user-status-1')
    assert updated['status'] == 'suspended'
    assert client.command('tenant.user.record_login', {
        'user_id': 'tenant-user-1', 'last_login_at': 200,
    }, 'tenant-user-login-1') == {'updated': True}
    listed = client.query('tenant.user.list', {
        'limit': 10, 'offset': 0, 'status': 'suspended',
    })
    assert [row['id'] for row in listed] == ['tenant-user-1']
    assert listed[0]['last_login_at'] == 200


def test_optimizer_aggregate_semantics(storage):
    client = storage.client
    assert client.command('optimizer.proposal.create', {
        'proposal_id': 'opt-contract', 'created_at': '2026-08-14T10:00:00',
        'title': 'Bound writer queue', 'rationale': 'protect memory',
        'action_type': 'set_limit', 'action_args': '{"limit":200}',
        'severity': 'high', 'confidence': 0.9, 'evidence': '["metric"]',
        'status': 'pending_review', 'status_reason': '',
    }, 'optimizer-proposal-contract') == {'proposal_id': 'opt-contract'}
    proposal = client.query(
        'optimizer.proposal.get', {'proposal_id': 'opt-contract'})
    assert proposal['title'] == 'Bound writer queue'
    assert proposal['confidence'] == 0.9
    assert len(client.query('optimizer.proposal.list', {
        'status': 'pending_review', 'limit': 10,
    })) == 1
    client.command('optimizer.proposal.update', {
        'proposal_id': 'opt-contract', 'status': 'applied', 'reason': 'test',
    }, 'optimizer-proposal-applied')
    client.command('optimizer.action.record', {
        'log_id': 'act-contract', 'proposal_id': 'opt-contract',
        'applied_at': '2026-08-14T10:01:00',
        'expires_at': '2026-08-15T10:01:00', 'pre_metric': '{}',
    }, 'optimizer-action-contract')
    client.command('optimizer.action.outcome', {
        'log_id': 'act-contract', 'outcome_metric': '{"ok":true}',
        'recorded_at': '2026-08-14T11:00:00',
    }, 'optimizer-outcome-contract')
    expired = client.query('optimizer.action.expired', {
        'now_iso': '2026-08-16T00:00:00',
    })
    assert [row['id'] for row in expired] == ['act-contract']
    assert expired[0]['p_status'] == 'applied'
    assert client.query('optimizer.action.for_proposal', {
        'proposal_id': 'opt-contract',
    })['outcome_metric'] == '{"ok":true}'
    client.command('optimizer.action.revert', {
        'log_id': 'act-contract', 'reverted_at': '2026-08-16T00:00:01',
        'reason': 'expired',
    }, 'optimizer-revert-contract')
    assert client.query('optimizer.action.list', {
        'include_reverted': False, 'limit': 10,
    }) == []
    assert len(client.query('optimizer.action.list', {
        'include_reverted': True, 'limit': 10,
    })) == 1


def test_log_aggregate_batch_query_and_sweep(storage):
    client = storage.client
    rows = [
        {'fingerprint': 'fp-active', 'level': 'ERROR', 'logger': 'test',
         'template': 'capacity reached 95%', 'sample': 'sample', 'count': 3,
         'first_seen': 100, 'last_seen': 300},
        {'fingerprint': 'fp-stale', 'level': 'WARNING', 'logger': 'test',
         'template': 'stale event', 'sample': 'old', 'count': 1,
         'first_seen': 100, 'last_seen': 100},
    ]
    assert client.command(
        'log_aggregate.flush', {'rows': rows}, None,
        priority='event') == {'flushed': 2, 'swept': 0}
    client.command('log_aggregate.flush', {'rows': [{
        **rows[0], 'count': 2, 'last_seen': 400,
    }]}, None, priority='event')
    queried = client.query('log_aggregate.query', {
        'level': 'ERROR', 'sort': 'count', 'limit': 10, 'q': '95%',
    })
    assert queried['total_rows'] == 1
    assert queried['total_events'] == 5
    assert queried['items'][0]['count'] == 5
    swept = client.command(
        'log_aggregate.flush', {'rows': [], 'cutoff_ms': 200}, None,
        priority='maintenance')
    assert swept == {'flushed': 0, 'swept': 1}
    assert client.query('log_aggregate.query', {
        'level': '', 'sort': 'last_seen', 'limit': 10, 'q': '',
    })['total_rows'] == 1


def test_plugin_manifest_and_named_operations(storage):
    manifest = {
        'namespace': 'example.notes',
        'version': 1,
        'tables': [{
            'name': 'notes',
            'columns': [
                {'name': 'id', 'type': 'string', 'required': True},
                {'name': 'title', 'type': 'string', 'required': True},
                {'name': 'done', 'type': 'boolean'},
            ],
            'primary_key': ['id'],
            'indexes': [{'name': 'by_done', 'columns': ['done']}],
        }],
        'operations': [
            {'name': 'get_note', 'kind': 'query', 'action': 'get', 'table': 'notes'},
            {'name': 'list_notes', 'kind': 'query', 'action': 'list', 'table': 'notes'},
            {'name': 'put_note', 'kind': 'command', 'action': 'put', 'table': 'notes'},
            {'name': 'delete_note', 'kind': 'command', 'action': 'delete', 'table': 'notes'},
        ],
    }
    assert storage.client.command(
        'plugin.register', {'manifest': manifest}, 'register-notes') == {
            'namespace': 'example.notes', 'version': 1,
        }
    put = storage.client.command(
        'plugin.example.notes.put_note',
        {'document': {'id': 'n1', 'title': 'First', 'done': False}},
        'put-note-1',
    )
    assert put['version'] == 1
    assert storage.client.query(
        'plugin.example.notes.get_note', {'id': 'n1'})['document']['title'] == 'First'
    listed = storage.client.query(
        'plugin.example.notes.list_notes', {'filters': {'done': False}})
    assert [row['document']['id'] for row in listed] == ['n1']


def test_plugin_incompatible_manifest_fails_closed(storage):
    with pytest.raises(StorageError) as raised:
        storage.client.command(
            'plugin.register',
            {'manifest': {
                'namespace': 'example.bad', 'version': 1,
                'tables': [{'name': 'x', 'columns': [], 'primary_key': ['id']}],
                'operations': [],
            }},
            'register-bad',
        )
    assert raised.value.code == 'plugin_storage_incompatible'


def test_wrong_token_and_unknown_operation_are_protocol_errors(storage):
    bad = StorageClient(
        storage.client.endpoint[0], storage.client.endpoint[1], 'x' * 48)
    with pytest.raises(StorageError) as auth_error:
        bad.health()
    assert auth_error.value.code == 'database_protocol_error'
    with pytest.raises(StorageError) as operation_error:
        storage.client.query('arbitrary.sql', {'sql': 'DROP TABLE users'})
    assert operation_error.value.code == 'database_protocol_error'


def test_malformed_semantic_inputs_are_classified_consistently(storage):
    with pytest.raises(StorageError) as bad_limit:
        storage.client.query(
            'record.list', {'namespace': 'contract', 'limit': 'many'})
    assert bad_limit.value.code == 'database_protocol_error'

    with pytest.raises(StorageError) as bad_version:
        storage.client.command(
            'record.put',
            {'namespace': 'contract', 'key': 'bad-version', 'value': 1,
             'expected_version': 'zero'},
            'bad-version',
        )
    assert bad_version.value.code == 'database_protocol_error'

    with pytest.raises(StorageError) as bad_priority:
        storage.client.command(
            'event.append',
            {'task_id': 'task-priority', 'sequence': 1, 'event': {}},
            None,
            priority='urgent',
        )
    assert bad_priority.value.code == 'database_protocol_error'

    with pytest.raises(StorageError) as bad_command_id:
        storage.client.command(
            'record.put',
            {'namespace': 'contract', 'key': 'bad-id', 'value': 1},
            ['not', 'text'],  # type: ignore[arg-type]
        )
    assert bad_command_id.value.code == 'database_protocol_error'


def test_backup_is_verified_and_kept_inside_project(storage, tmp_path):
    storage.client.command(
        'record.put', {'namespace': 'backup', 'key': 'kept', 'value': 1},
        'backup-seed')
    result = storage.client.maintenance('system.backup', deadline=30)
    target = (tmp_path / result['backup']).resolve()
    assert result['ok'] is True
    assert target.exists()
    target.relative_to((tmp_path / 'data' / 'backups').resolve())
    if target.is_file():
        assert target.stat().st_size == result['bytes']
    else:
        assert result['bytes'] > 0


def test_sidecar_crash_revokes_readiness_without_backend_switch(tmp_path, monkeypatch):
    monkeypatch.setenv('TOFU_STORAGE_SQLITE_READ_POOL', '1')
    crashed = threading.Event()
    codes = []
    supervisor = StorageSupervisor(
        project_root=tmp_path,
        backend='sqlite',
        startup_timeout=15,
        on_crash=lambda code: (codes.append(code), crashed.set()),
    )
    supervisor.start()
    ready_status = supervisor.status()
    assert ready_status['ready'] is True
    assert ready_status['state'] == 'ready'
    assert ready_status['backend'] == 'sqlite'
    assert isinstance(ready_status['pid'], int)
    process = supervisor._process
    assert process is not None
    process.kill()
    assert crashed.wait(5)
    assert supervisor.ready is False
    crashed_status = supervisor.status()
    assert crashed_status['state'] == 'exited'
    assert crashed_status['last_exit_code'] is not None
    assert codes
    # No fallback client/backend is synthesized after the crash.
    with pytest.raises(RuntimeError, match='not ready'):
        _ = supervisor.client


def test_runtime_fences_then_restarts_and_rehandshakes_same_backend(
        tmp_path, monkeypatch):
    monkeypatch.setenv('TOFU_STORAGE_SQLITE_READ_POOL', '1')
    fenced = threading.Event()
    supervisor = StorageSupervisor(
        project_root=tmp_path, backend='sqlite', startup_timeout=15)
    runtime = StorageRuntime(supervisor, on_write_fence=fenced.set)
    try:
        runtime.start().command(
            'record.put',
            {'namespace': 'restart', 'key': 'durable', 'value': True},
            'restart-seed',
        )
        process = supervisor._process
        assert process is not None
        process.kill()
        assert fenced.wait(5)
        deadline = time.monotonic() + 15
        while not runtime.ready and time.monotonic() < deadline:
            time.sleep(0.05)
        assert runtime.ready is True
        status = runtime.status()
        assert status['state'] == 'ready'
        assert status['restart_attempts'] >= 1
        assert status['last_exit_code'] is not None
        assert runtime.client().health()['backend'] == 'sqlite'
        assert runtime.client().query(
            'record.get', {'namespace': 'restart', 'key': 'durable'})['value'] is True
    finally:
        runtime.stop()


@pytest.mark.skipif(
    os.environ.get('TOFU_STORAGE_TEST_POSTGRES') != '1',
    reason='real PostgreSQL contract is opt-in',
)
def test_postgres_kill_adopts_verified_project_cluster(tmp_path, monkeypatch):
    monkeypatch.setenv('TOFU_STORAGE_PG_READ_POOL', '1')
    monkeypatch.setenv('TOFU_STORAGE_PG_WRITE_POOL', '1')
    fenced = threading.Event()
    supervisor = StorageSupervisor(
        project_root=tmp_path, backend='postgres', startup_timeout=60)
    runtime = StorageRuntime(supervisor, on_write_fence=fenced.set)
    try:
        runtime.start().command(
            'record.put',
            {'namespace': 'pg-restart', 'key': 'durable', 'value': True},
            'pg-restart-seed',
        )
        process = supervisor._process
        assert process is not None
        process.kill()
        assert fenced.wait(10)
        deadline = time.monotonic() + 60
        while not runtime.ready and time.monotonic() < deadline:
            time.sleep(0.05)
        assert runtime.ready
        assert runtime.client().query(
            'record.get',
            {'namespace': 'pg-restart', 'key': 'durable'},
        )['value'] is True
    finally:
        runtime.stop()
    assert not (tmp_path / 'data' / 'pgdata' / 'postmaster.pid').exists()


def test_backend_selector_is_strict_and_defaults_to_sqlite(tmp_path, monkeypatch):
    from lib.storage_sidecar.config import SidecarConfig

    monkeypatch.setenv('TOFU_STORAGE_TOKEN', 't' * 48)
    monkeypatch.setenv('TOFU_STORAGE_PROJECT_ROOT', str(tmp_path))
    monkeypatch.setenv('TOFU_STORAGE_ALLOW_PROJECT_OVERRIDE', '1')
    monkeypatch.delenv('TOFU_DB_BACKEND', raising=False)
    assert SidecarConfig.from_environment().backend == 'sqlite'
    monkeypatch.setenv('TOFU_DB_BACKEND', 'pg')
    with pytest.raises(RuntimeError, match='exactly sqlite or postgres'):
        SidecarConfig.from_environment()
