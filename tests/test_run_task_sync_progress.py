"""Tests for run_task_sync progress forwarding.

The Feishu pipeline passes a ``send_progress_fn`` through to
``run_task_sync(progress_fn=...)`` so a non-streaming consumer gets live
tool-start progress while the task runs (previously the callback was accepted
and silently dropped with a "not yet wired up" debug log).

These tests exercise the pure helpers — ``_format_progress_event`` and
``_drain_progress`` — deterministically, without spawning a real task.

Run:  pytest tests/test_run_task_sync_progress.py -m unit
"""
from __future__ import annotations

import os
import sys
import threading

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@pytest.mark.unit
class TestFormatProgressEvent:
    def test_tool_start_with_query(self):
        from lib.tasks_pkg.sync_run import _format_progress_event
        ev = {'type': 'tool_start', 'toolName': 'web_search',
              'query': 'latest news'}
        assert _format_progress_event(ev) == 'Running web_search: latest news'

    def test_tool_start_without_query(self):
        from lib.tasks_pkg.sync_run import _format_progress_event
        ev = {'type': 'tool_start', 'toolName': 'fetch_url'}
        assert _format_progress_event(ev) == 'Running fetch_url…'

    def test_non_tool_event_is_empty(self):
        from lib.tasks_pkg.sync_run import _format_progress_event
        assert _format_progress_event({'type': 'delta', 'content': 'hi'}) == ''
        assert _format_progress_event({'type': 'done'}) == ''
        assert _format_progress_event({'type': 'round_usage'}) == ''

    def test_non_dict_is_empty(self):
        from lib.tasks_pkg.sync_run import _format_progress_event
        assert _format_progress_event(None) == ''
        assert _format_progress_event('tool_start') == ''


def _fake_task(events):
    return {'events': list(events), 'events_lock': threading.Lock()}


@pytest.mark.unit
class TestDrainProgress:
    def test_forwards_only_tool_start_and_advances_cursor(self):
        from lib.tasks_pkg.sync_run import _drain_progress
        task = _fake_task([
            {'type': 'tool_start', 'toolName': 'web_search', 'query': 'q1'},
            {'type': 'delta', 'content': 'noise'},
            {'type': 'tool_start', 'toolName': 'fetch_url'},
        ])
        seen = []
        new_cursor = _drain_progress(task, 0, seen.append)
        # Cursor advances past ALL drained events (not just forwarded ones).
        assert new_cursor == 3
        # Only the two tool_start events produced progress lines.
        assert seen == ['Running web_search: q1', 'Running fetch_url…']

    def test_cursor_skips_already_seen(self):
        from lib.tasks_pkg.sync_run import _drain_progress
        task = _fake_task([
            {'type': 'tool_start', 'toolName': 'a'},
            {'type': 'tool_start', 'toolName': 'b'},
        ])
        seen = []
        # Start at cursor=1 → only the second event is considered.
        new_cursor = _drain_progress(task, 1, seen.append)
        assert new_cursor == 2
        assert seen == ['Running b…']

    def test_callback_error_is_swallowed(self):
        from lib.tasks_pkg.sync_run import _drain_progress
        task = _fake_task([{'type': 'tool_start', 'toolName': 'a'}])

        def boom(_line):
            raise RuntimeError('progress sink down')

        # Must NOT raise — progress reporting can't break the task.
        new_cursor = _drain_progress(task, 0, boom)
        assert new_cursor == 1

    def test_no_new_events_is_noop(self):
        from lib.tasks_pkg.sync_run import _drain_progress
        task = _fake_task([{'type': 'tool_start', 'toolName': 'a'}])
        seen = []
        new_cursor = _drain_progress(task, 1, seen.append)
        assert new_cursor == 1
        assert seen == []


@pytest.mark.unit
class TestPipelineWiring:
    """The Feishu pipeline must pass send_progress_fn through to
    run_task_sync(progress_fn=...) — not drop it."""

    def test_send_progress_fn_reaches_run_task_sync(self, monkeypatch):
        import lib.feishu.pipeline as pipeline

        captured = {}

        def fake_run_task_sync(config, *, progress_fn=None, **kw):
            captured['progress_fn'] = progress_fn
            return 'ok'

        # Stub the conversation/persistence side-effects.
        monkeypatch.setattr(pipeline, 'append_message', lambda *a, **k: None)
        monkeypatch.setattr(pipeline, 'get_history', lambda *a, **k: [])
        monkeypatch.setattr(pipeline, 'get_model', lambda *a, **k: 'qwen-plus')
        monkeypatch.setattr(pipeline, 'get_mode', lambda *a, **k: 'chat')
        monkeypatch.setattr(pipeline, 'get_project', lambda *a, **k: '')
        monkeypatch.setattr(pipeline, 'get_conv_id', lambda *a, **k: 'c1')
        monkeypatch.setattr(pipeline, 'resolve_owner_user_id', lambda *_: 7)
        monkeypatch.setattr(pipeline, 'persist_exchange', lambda *a, **k: True)

        import lib.tasks_pkg.sync_run as sync_run
        monkeypatch.setattr(sync_run, 'run_task_sync', fake_run_task_sync)

        sentinel = lambda line: None  # noqa: E731
        pipeline.run_task_pipeline('user-1', 'hello', send_progress_fn=sentinel)
        assert captured['progress_fn'] is sentinel
