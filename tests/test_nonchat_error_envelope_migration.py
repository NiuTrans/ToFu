"""Dedicated UI failures use the same typed envelope boundary as chat."""

from __future__ import annotations

import time

import pytest

from lib.error_envelope import is_envelope


pytestmark = pytest.mark.unit


def test_file_history_unhandled_failure_is_an_envelope(tmp_path, monkeypatch):
    from lib.file_history import api

    def fail_lookup(*_args, **_kwargs):
        raise OSError('history store unavailable')

    monkeypatch.setattr(api, 'find_snapshot_with_previous', fail_lookup)
    result = api.rewind_to(str(tmp_path), 'snapshot-1')

    assert is_envelope(result['error'])
    assert result['error']['context'] == 'file-history-rewind'
    assert 'history store unavailable' in result['error']['detail']


def test_provider_probe_worker_failure_is_an_envelope(monkeypatch):
    import lib.provider_probe as probe

    monkeypatch.setattr(probe, 'persist_probe_task', lambda _task: None)
    task = {
        'provider_id': 'provider-1',
        'status': 'running',
        'started_at': 1,
        'finished_at': None,
        'total': 0,
        'done_count': 0,
        'attempts': 1,
        'cells': {},
        'summary': {'ok': 0, 'disable': 0},
        'error': None,
        '_abort': False,
        '_base_url': 'https://example.invalid',
        '_extra_headers': {},
        '_adapter': {'agent_id': 'test'},
    }

    # An empty work batch makes ThreadPoolExecutor reject max_workers=0 and
    # drives the background task's outer failure boundary without networking.
    probe.run_cell_probe_task(task, [], timeout=1)

    assert task['status'] == 'error'
    assert is_envelope(task['error'])
    assert task['error']['context'] == 'provider-cell-probe'


def test_vlm_background_failure_is_an_envelope(monkeypatch):
    from lib.pdf_parser.vlm import _tasks

    def fail_parse(*_args, **_kwargs):
        raise RuntimeError('vision parser unavailable')

    monkeypatch.setattr(_tasks, 'vlm_parse_pdf', fail_parse)
    task_id = _tasks.start_vlm_task(
        b'pdf',
        filename='broken.pdf',
        user_id=1,
    )
    deadline = time.monotonic() + 3
    task = _tasks.get_vlm_task(task_id, user_id=1)
    while task and task['status'] == 'processing' and time.monotonic() < deadline:
        time.sleep(0.01)
        task = _tasks.get_vlm_task(task_id, user_id=1)
    try:
        assert task is not None
        assert task['status'] == 'error'
        assert is_envelope(task['error'])
        assert task['error']['context'] == 'vlm-pdf-parse'
    finally:
        with _tasks._vlm_lock:
            _tasks._vlm_tasks.pop(task_id, None)


def test_retained_ui_consumers_normalize_nonchat_error_fields():
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    # The consumers moved out of the generated app-runtime.js monolith into
    # their retained section owners during the classic-to-ESM migration.
    sections = (
        root / 'frontend/src/runtime/sections/settings/oauth.js',
        root / 'frontend/src/runtime/sections/upload.js',
    )
    source = '\n'.join(path.read_text(encoding='utf-8') for path in sections)

    for contract in (
        'entry.vlmError = errorEnvelopeMessage(task.error);',
        'badge.title = errorEnvelopeMessage(status.error);',
        'error: errorEnvelopeMessage(data.error) || String(data.error)',
    ):
        assert contract in source
