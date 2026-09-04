"""Contracts for the incremental Quart/Vite/observability modernization."""

from __future__ import annotations

import ast
import asyncio
import json
from pathlib import Path
import subprocess
import threading
from types import SimpleNamespace

import pytest


pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[1]
_AUDIT_SYNTHETIC_REPO_PATHS = {
    'static/vite/assets/main-abc.css',
    'static/vite/assets/main-abc.js',
}


def test_request_ids_are_context_local_between_coroutines():
    from lib.log import req_id, set_req_id

    async def run():
        ready = asyncio.Event()
        seen = []

        async def worker(value):
            set_req_id(value)
            ready.set()
            await asyncio.sleep(0)
            seen.append(req_id())

        await asyncio.gather(worker('request-a'), worker('request-b'))
        return seen

    assert sorted(asyncio.run(run())) == ['request-a', 'request-b']


def test_http_metrics_collapse_dynamic_ids_and_render_histogram():
    from lib.observability import (
        prometheus_lines,
        record_http_request,
        reset_for_tests,
    )

    reset_for_tests()
    record_http_request('GET', '/api/task/0123456789abcdef', 200, 0.125)
    text = '\n'.join(prometheus_lines())
    assert 'tofu_http_requests_total' in text
    assert 'tofu_http_request_duration_seconds_bucket' in text
    assert '/api/task/&lt;id&gt;' not in text  # Prometheus text is not HTML.
    assert 'route="/api/task/<id>"' in text
    assert '0123456789abcdef' not in text


def test_terminal_llm_metrics_cover_slo_cost_and_cache_dimensions():
    from lib.observability import prometheus_lines, record_llm_task, reset_for_tests

    reset_for_tests()
    record_llm_task(
        'model-stable', 'provider-stable', 2.5, 3,
        {'input_tokens': 100, 'cache_read_input_tokens': 80,
         'output_tokens': 20},
        0.012, 'success',
    )
    text = '\n'.join(prometheus_lines())
    for metric in (
        'tofu_llm_total_duration_seconds', 'tofu_llm_api_rounds',
        'tofu_llm_context_tokens', 'tofu_llm_estimated_cost_usd',
        'tofu_llm_cache_hit_tasks_total',
    ):
        assert metric in text
    assert 'requestId' not in text


def test_metrics_snapshot_is_reused_for_five_seconds(monkeypatch):
    import routes.metrics as metrics

    calls = []
    metrics.clear_metrics_snapshot_cache()
    monkeypatch.setattr(metrics, '_collect_usage_metrics',
                        lambda out: calls.append('usage'))
    monkeypatch.setattr(metrics, '_collect_task_metrics',
                        lambda out: calls.append('tasks'))
    monkeypatch.setattr(metrics, '_collect_infra_metrics',
                        lambda out: calls.append('infra'))
    monkeypatch.setattr('lib.observability.prometheus_lines', lambda: ['x 1'])

    first, first_hit = metrics._metrics_snapshot()
    second, second_hit = metrics._metrics_snapshot()
    assert first == second == 'x 1\n'
    assert first_hit is False
    assert second_hit is True
    assert calls == ['usage', 'tasks', 'infra']
    metrics.clear_metrics_snapshot_cache()


def test_idempotency_metrics_expose_capacity_retention_and_evictions(
        monkeypatch):
    import routes.metrics as metrics

    monkeypatch.setattr('lib.idempotency.cache_stats', lambda: {
        'size': 12,
        'max_size': 100,
        'ttl': 3600,
        'hits': 7,
        'misses': 3,
        'expired_evicts': 2,
        'size_evicts': 1,
    })
    out = []
    metrics._collect_infra_metrics(out)
    text = '\n'.join(out)
    for metric in (
        'tofu_idempotency_cache_size',
        'tofu_idempotency_cache_capacity',
        'tofu_idempotency_cache_ttl_seconds',
        'tofu_idempotency_cache_hits_total',
        'tofu_idempotency_cache_misses_total',
        'tofu_idempotency_cache_evictions_total{reason="expired"} 2',
        'tofu_idempotency_cache_evictions_total{reason="capacity"} 1',
    ):
        assert metric in text


def test_memory_rate_limit_metrics_expose_both_resident_bounds(monkeypatch):
    import routes.metrics as metrics

    monkeypatch.setattr('lib.rate_limit_api.api_rate_limit_stats', lambda: {
        'entries': 7,
        'capacity': 1_024,
        'capacity_evictions': 6,
    })
    monkeypatch.setattr('lib.rate_limit_store.rate_limit_store_stats', lambda: {
        'backend': 'memory',
        'buckets': 12,
        'bucket_capacity': 1_024,
        'events': 80,
        'event_capacity': 131_072,
        'expired_bucket_evictions': 2,
        'bucket_capacity_evictions': 3,
        'event_capacity_evictions': 4,
        'event_capacity_rejections': 5,
    })
    out = []
    metrics._collect_infra_metrics(out)
    text = '\n'.join(out)
    for metric in (
        'tofu_rate_limit_buckets 7',
        'tofu_rate_limit_bucket_capacity 1024',
        'tofu_rate_limit_bucket_evictions_total 6',
        'tofu_rate_limit_memory_buckets 12',
        'tofu_rate_limit_memory_bucket_capacity 1024',
        'tofu_rate_limit_memory_events 80',
        'tofu_rate_limit_memory_event_capacity 131072',
        'tofu_rate_limit_memory_bucket_evictions_total{reason="expired"} 2',
        'tofu_rate_limit_memory_bucket_evictions_total{reason="bucket_capacity"} 3',
        'tofu_rate_limit_memory_bucket_evictions_total{reason="event_capacity"} 4',
        'tofu_rate_limit_memory_event_rejections_total 5',
    ):
        assert metric in text


def test_task_metrics_expose_registry_and_event_retention(monkeypatch):
    import routes.metrics as metrics

    class Runtime:
        @staticmethod
        def stats():
            return {
                'kind': 'paper-report',
                'total': 2,
                'pending': 0,
                'running': 1,
                'done': 1,
                'error': 0,
                'aborted': 0,
            }

        @staticmethod
        def retention_stats():
            return {
                'tasks': 2,
                'max_tasks': 64,
                'ttl_seconds': 1800,
                'events': 3,
                'event_retained_bytes': 4096,
                'max_events_per_task': 256,
                'event_buffer_byte_capacity_per_task': 1_048_576,
                'event_max_bytes': 2_097_152,
                'event_retention_hard_capacity_per_task': 2_097_152,
                'over_capacity': 0,
            }

    monkeypatch.setattr(
        'routes.api_v1.tasks._registries', lambda: {'paper-report': Runtime()})
    monkeypatch.setattr(
        'lib.production.runtime.production_retention_stats',
        lambda: [{
            'kind': 'paper-podcast',
            'size': 7,
            'capacity': 64,
            'over_capacity': 0,
            'evictions': {'orphan': 2, 'terminal': 3, 'ttl': 4},
        }],
    )
    out = []
    metrics._collect_task_metrics(out)
    text = '\n'.join(out)
    for sample in (
        'tofu_task_registry_size{kind="paper-report"} 2',
        'tofu_task_registry_capacity{kind="paper-report"} 64',
        'tofu_task_registry_ttl_seconds{kind="paper-report"} 1800',
        'tofu_task_events_retained{kind="paper-report"} 3',
        'tofu_task_event_retention_limit{kind="paper-report"} 256',
        'tofu_task_event_retained_bytes{kind="paper-report"} 4096',
        'tofu_task_event_buffer_bytes_per_task{kind="paper-report"} 1048576',
        'tofu_task_event_max_bytes{kind="paper-report"} 2097152',
        'tofu_task_event_hard_bytes_per_task{kind="paper-report"} 2097152',
        'tofu_task_dedup_index_size{kind="paper-podcast"} 7',
        'tofu_task_dedup_index_capacity{kind="paper-podcast"} 64',
        'tofu_task_dedup_index_evictions_total{kind="paper-podcast",reason="ttl"} 4',
    ):
        assert sample in text


def test_task_events_carry_correlation_envelope():
    from lib.agent_core.task_runtime import TaskRuntime
    from lib.log import set_req_id

    set_req_id('browser-page-7')
    runtime = TaskRuntime('test-modern', push_channel='')
    task = runtime.create(user_id=1, task_id='task-modern')
    runtime.append_event(task['id'], {'type': 'phase'})

    event = task['events'][0]
    assert event == {
        'type': 'phase',
        'taskId': 'task-modern',
        'requestId': 'browser-page-7',
        'seq': 0,
    }
    replay = runtime.poll(task['id'], 0)
    assert replay['taskId'] == 'task-modern'
    assert replay['requestId'] == 'browser-page-7'
    assert replay['cursor']['next'] == 1
    set_req_id('')


def test_task_worker_reseeds_only_the_captured_request_id():
    """A fresh worker Context keeps correlation but not Quart request state."""
    from lib.agent_core.task_runtime import TaskRuntime
    from lib.log import req_id, set_req_id

    runtime = TaskRuntime('worker-correlation', push_channel='')
    set_req_id('browser-page-8')
    task = runtime.create(user_id=1, task_id='task-worker-correlation')
    # Model the route teardown happening before the background worker starts.
    set_req_id('')
    completed = threading.Event()
    observed = {}

    def worker():
        observed['requestId'] = req_id()
        runtime.finish(task['id'])
        completed.set()

    runtime.spawn(task['id'], worker)
    assert completed.wait(2), 'background worker did not complete'
    assert observed == {'requestId': 'browser-page-8'}
    assert req_id() == '', 'worker correlation leaked back to the caller'


def test_task_worker_records_real_queue_wait(monkeypatch):
    from lib.agent_core.task_runtime import TaskRuntime

    observed = []
    monkeypatch.setattr(
        'lib.observability.record_task_queue_wait',
        lambda kind, seconds: observed.append((kind, seconds)),
    )
    runtime = TaskRuntime('paper-report', push_channel='')
    task = runtime.create(user_id=1, task_id='queue-wait-task')
    completed = threading.Event()

    def worker():
        runtime.finish(task['id'])
        completed.set()

    runtime.spawn(task['id'], worker)
    assert completed.wait(2), 'background worker did not complete'
    assert len(observed) == 1
    assert observed[0][0] == 'paper-report'
    assert observed[0][1] >= 0


def test_task_runtime_bounds_replay_events_without_reusing_sequences():
    from lib.agent_core.task_runtime import TaskRuntime
    from lib.observability import prometheus_lines, reset_for_tests

    reset_for_tests()
    runtime = TaskRuntime(
        'bounded-replay', max_events=3, max_tasks=4, push_channel='')
    task = runtime.create(user_id=1, task_id='bounded-task')
    for index in range(5):
        assert runtime.append_event(
            task['id'], {'type': 'progress', 'index': index}) == index

    assert [event['seq'] for event in task['events']] == [2, 3, 4]
    reset = runtime.poll(task['id'], 0)
    assert [event['index'] for event in reset['events']] == [2, 3, 4]
    assert reset['next_cursor'] == 5
    assert reset['cursor'] == {'requested': 0, 'next': 5, 'reset': True}

    incremental = runtime.poll(task['id'], 3)
    assert [event['index'] for event in incremental['events']] == [3, 4]
    assert incremental['cursor'] == {
        'requested': 3, 'next': 5, 'reset': False,
    }
    assert 'tofu_task_event_evictions_total{kind="bounded-replay"} 2.0' \
        in '\n'.join(prometheus_lines())


def test_task_runtime_capacity_evicts_only_terminal_records():
    from lib.agent_core.task_runtime import TaskRuntime

    runtime = TaskRuntime(
        'bounded-registry', max_tasks=2, max_events=2,
        max_event_buffer_bytes=1024, max_event_bytes=2048,
        push_channel='')
    first = runtime.create(user_id=1, task_id='finished-first')
    runtime.finish(first['id'])
    active = runtime.create(user_id=1, task_id='active-second')
    newest = runtime.create(user_id=1, task_id='newest-third')

    assert runtime.get(first['id']) is None
    assert runtime.get(active['id']) is active
    assert runtime.get(newest['id']) is newest
    assert runtime.retention_stats() == {
        'tasks': 2,
        'max_tasks': 2,
        'ttl_seconds': 3600,
        'events': 0,
        'event_retained_bytes': 0,
        'max_events_per_task': 2,
        'event_buffer_byte_capacity_per_task': 1024,
        'event_max_bytes': 2048,
        'event_retention_hard_capacity_per_task': 2048,
        'over_capacity': 0,
    }


def test_vite_manifest_tags_and_corruption_fail_closed(tmp_path, monkeypatch):
    import lib.vite_assets as assets

    output = tmp_path / 'assets'
    output.mkdir()
    for name in ('main-abc.js', 'shared-abc.js', 'main-abc.css'):
        (output / name).write_text('/* test asset */', encoding='utf-8')
    manifest = tmp_path / 'manifest.json'
    manifest.write_text(json.dumps({
        assets.VITE_ENTRY: {
            'file': 'assets/main-abc.js',
            'isEntry': True,
            'imports': ['shared.ts'],
            'css': ['assets/main-abc.css'],
            assets.I18N_CATALOG_DIGEST_FIELD:
                assets._source_i18n_catalog_digest(),
            assets.VITE_AUTHORING_DIGEST_FIELD: 'a' * 64,
        },
        'shared.ts': {'file': 'assets/shared-abc.js'},
    }), encoding='utf-8')
    monkeypatch.setattr(assets, 'VITE_OUT_DIR', str(tmp_path))
    monkeypatch.setattr(assets, 'VITE_MANIFEST', str(manifest))
    monkeypatch.delenv('TOFU_VITE_DEV_SERVER', raising=False)
    assets.clear_vite_asset_cache()
    tags = assets.get_vite_asset_tags()
    assert 'type="module"' in tags
    assert 'static/vite/assets/main-abc.js' in tags
    assert 'rel="modulepreload"' in tags
    assert 'static/vite/assets/main-abc.css' in tags

    manifest.write_text(json.dumps({
        assets.VITE_ENTRY: {'file': '../escape.js', 'isEntry': True},
    }), encoding='utf-8')
    assets.clear_vite_asset_cache()
    with pytest.raises(assets.ViteAssetError):
        assets.get_vite_asset_tags()


def test_application_code_uses_native_quart_imports():
    offenders = []
    # Respect the repository ignore rules.  ``Path.rglob`` also descends into
    # large ignored caches that can exist under these directories in local
    # development worktrees.
    paths = subprocess.check_output(
        ['rg', '--files', 'lib', 'routes', '-g', '*.py'],
        cwd=ROOT,
        text=True,
    ).splitlines()
    for relative in paths:
        source = (ROOT / relative).read_text(encoding='utf-8')
        if any(line.lstrip().startswith(('from flask import ',
                                        'import flask'))
               for line in source.splitlines()):
            offenders.append(relative)
    assert offenders == []

def test_test_suite_does_not_install_flask_to_quart_shims():
    """Keep test imports from masking production framework regressions."""
    offenders = []
    paths = subprocess.check_output(
        ['rg', '--files', 'tests', '-g', '*.py'],
        cwd=ROOT,
        text=True,
    ).splitlines()
    for relative in paths:
        tree = ast.parse(
            (ROOT / relative).read_text(encoding='utf-8'),
            filename=relative,
        )
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Assign, ast.AnnAssign)):
                continue
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if not isinstance(target, ast.Subscript):
                    continue
                owner = target.value
                key = target.slice
                if (isinstance(owner, ast.Attribute)
                        and isinstance(owner.value, ast.Name)
                        and owner.value.id == 'sys'
                        and owner.attr == 'modules'
                        and isinstance(key, ast.Constant)
                        and key.value == 'flask'):
                    offenders.append(f'{relative}:{node.lineno}')
    assert offenders == []
