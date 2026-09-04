"""Regression: streaming pre-exec ``fetch_url`` delegates to the authoritative
``handlers.search._core._fetch_url_one`` — so binary file assets and text assets are
handled IDENTICALLY to the serial pipeline.

Background
----------
``StreamingToolAccumulator._execute_one('fetch_url', ...)`` pre-executes the
tool while the model is still streaming and injects the result into
``task['_tool_result_cache']`` as AUTHORITATIVE — the serial pipeline then
finds the cache hit and SKIPS re-execution. Historically this path used the
old text-only ``fetch_page_content`` directly, so a binary URL (image / PDF /
archive) that the serial ``_fetch_url_one`` would stage to ``data/fetched/``
for ``read_files`` silently returned nothing when pre-executed — and because
the empty result was cached, the loss was invisible.

The fix routes the streaming path through ``_fetch_url_one`` (single source of
truth). These tests pin that delegation for both single-URL and batch modes,
and a double-neuter proves the assertion is load-bearing.
"""
from __future__ import annotations

import os
import sys
import time
from contextlib import contextmanager
from concurrent.futures import Future
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _make_task(tid='stream-fetch-test'):
    return {
        'id': tid,
        'aborted': False,
        'lastUserQuery': 'what is X',
        '_tool_result_cache': {},
    }


def _asset_item(url):
    """A ``_fetch_url_one`` return shaped like a staged BINARY asset — the
    exact case the old text-only path could not produce."""
    note = '[fetch_url] This URL is a file asset (image/png, 1,234 bytes) → /data/fetched/x.png'
    return {
        'url': url, 'page_content': note, 'is_pdf': False,
        'raw_chars': 1234, 'filtered_chars': len(note), 'error_msg': None,
        'saved_path': '/data/fetched/x.png', 'is_asset': True,
    }


@pytest.mark.unit
class TestStreamingFetchUrlDelegation:

    def test_single_url_routes_through_fetch_url_one(self):
        """Single-URL streaming pre-exec calls _fetch_url_one and surfaces its
        staged-asset content (proving binary-asset support)."""
        from lib.tasks_pkg.streaming_tool_executor import StreamingToolAccumulator
        acc = StreamingToolAccumulator(_make_task(), project_path='/tmp')

        url = 'https://example.com/diagram.png'
        with patch('lib.tasks_pkg.handlers.search._core._fetch_url_one',
                   return_value=_asset_item(url)) as m:
            out = acc._execute_one('fetch_url', {'url': url, 'reason': 'see the diagram'})

        # Delegation happened with the serial-handler arg contract:
        #   _fetch_url_one(url, user_question, fetch_reason=<'reason' arg>)
        assert m.call_count == 1
        args, kwargs = m.call_args
        assert args[0] == url
        assert args[1] == 'what is X'                 # lastUserQuery
        assert kwargs.get('fetch_reason') == 'see the diagram'
        # The staged-asset note reached the LLM content (old path returned "Failed").
        assert 'file asset' in out
        assert 'chars):' in out                        # "Content from <url> (N chars):"

    def test_single_url_failure_includes_error_detail(self):
        from lib.tasks_pkg.streaming_tool_executor import StreamingToolAccumulator
        acc = StreamingToolAccumulator(_make_task(), project_path='/tmp')
        fail = {
            'url': 'ftp://nope', 'page_content': None, 'is_pdf': False,
            'raw_chars': 0, 'filtered_chars': 0,
            'error_msg': 'Rejected: ftp:// scheme',
            'saved_path': None, 'is_asset': False,
        }
        with patch('lib.tasks_pkg.handlers.search._core._fetch_url_one', return_value=fail):
            out = acc._execute_one('fetch_url', {'url': 'ftp://nope'})
        assert out.startswith('Failed to fetch ftp://nope.')
        assert 'Rejected: ftp:// scheme' in out

    def test_batch_routes_through_fetch_url_one_with_empty_reason(self):
        """Batch mode calls _fetch_url_one per URL with fetch_reason='' (parity
        with the serial batch worker) and carries display_results for the UI."""
        from lib.tasks_pkg.streaming_tool_executor import (
            StreamingToolAccumulator, _ContentWithDisplayResults)
        acc = StreamingToolAccumulator(_make_task(), project_path='/tmp')

        urls = ['https://a.com/page', 'https://b.com/file.zip']

        def fake(u, user_question, fetch_reason):
            assert fetch_reason == ''                   # batch parity
            assert user_question == 'what is X'
            if u.endswith('.zip'):
                return _asset_item(u)                   # binary asset
            return {
                'url': u, 'page_content': 'hello page', 'is_pdf': False,
                'raw_chars': 10, 'filtered_chars': 10, 'error_msg': None,
                'saved_path': None, 'is_asset': False,
            }

        with patch('lib.tasks_pkg.handlers.search._core._fetch_url_one', side_effect=fake) as m:
            out = acc._execute_one('fetch_url', {'urls': urls, 'reason': 'ignored-in-batch'})

        assert m.call_count == 2
        assert isinstance(out, _ContentWithDisplayResults)
        assert 'hello page' in out
        assert 'file asset' in out                       # the .zip asset survived
        # One display row per URL, and the asset row is typed "File Asset".
        rows = out.display_results
        assert len(rows) == 2
        assert any(r.get('source') == 'File Asset' for r in rows)

    def test_double_neuter_delegation_is_load_bearing(self):
        """NEUTER: force _fetch_url_one to return an empty/failed result and
        assert the staged-asset content DISAPPEARS — proving the delegation is
        what carries binary-asset support (not some incidental code path)."""
        from lib.tasks_pkg.streaming_tool_executor import StreamingToolAccumulator
        acc = StreamingToolAccumulator(_make_task(), project_path='/tmp')
        url = 'https://example.com/diagram.png'
        neutered = {
            'url': url, 'page_content': None, 'is_pdf': False,
            'raw_chars': 0, 'filtered_chars': 0, 'error_msg': None,
            'saved_path': None, 'is_asset': False,
        }
        with patch('lib.tasks_pkg.handlers.search._core._fetch_url_one', return_value=neutered):
            out = acc._execute_one('fetch_url', {'url': url})
        assert 'file asset' not in out
        assert out.startswith('Failed to fetch')

    def test_streaming_fetch_binds_exact_task_browser_identity(self, monkeypatch):
        from lib.tasks_pkg.streaming_tool_executor import StreamingToolAccumulator

        task = _make_task()
        task['_userId'] = '41'
        task['config'] = {'browserClientId': 'browser-a'}
        bindings = []

        @contextmanager
        def fake_binding(*, user_id='', client_id='', required_capabilities=()):
            bindings.append((user_id, client_id, tuple(required_capabilities)))
            yield (str(user_id), str(client_id), 'Default')

        monkeypatch.setattr('lib.search_bridge.bind_search_browser', fake_binding)
        url = 'https://example.com/page'
        acc = StreamingToolAccumulator(task, project_path='/tmp')
        with patch(
                'lib.tasks_pkg.handlers.search._core._fetch_url_one',
                return_value={
                    'url': url, 'page_content': 'ok', 'is_pdf': False,
                    'raw_chars': 2, 'filtered_chars': 2, 'error_msg': None,
                    'saved_path': None, 'is_asset': False,
                }):
            out = acc._execute_one('fetch_url', {'url': url})

        assert str(out).endswith('\n\nok')
        # One selection binding plus the actual fetch binding. Both retain the
        # request owner/device instead of falling back to request globals.
        assert bindings == [
            ('41', 'browser-a', ()),
            ('41', 'browser-a', ()),
        ]

    def test_failed_streaming_fetch_is_not_injected_as_authoritative_cache(
            self):
        from lib.tasks_pkg.streaming_tool_executor import StreamingToolAccumulator

        task = _make_task()
        url = 'https://offline.example/file'
        failure = {
            'url': url, 'page_content': None, 'is_pdf': False,
            'raw_chars': 0, 'filtered_chars': 0,
            'error_msg': 'browser temporarily offline',
            'saved_path': None, 'is_asset': False,
        }
        acc = StreamingToolAccumulator(task, project_path='/tmp')
        with patch(
                'lib.tasks_pkg.handlers.search._core._fetch_url_one',
                return_value=failure):
            content = acc._execute_one('fetch_url', {'url': url})
        assert getattr(content, 'cacheable') is False

        future = Future()
        future.set_result(content)
        acc._futures['tc-failed'] = (
            future, 'fetch_url', {'url': url}, time.time())
        acc._submitted_count = 1

        assert acc.inject_into_cache(task) == 0
        assert task['_tool_result_cache'] == {}

    def test_serial_fetch_failure_marks_outcome_non_cacheable(self, monkeypatch):
        import lib.tasks_pkg.handlers.search._core as core
        import lib.tasks_pkg.handlers.search._handlers as handlers

        url = 'https://offline.example/page'
        monkeypatch.setattr(core, '_fetch_url_one', lambda *_a, **_k: {
            'url': url, 'page_content': None, 'is_pdf': False,
            'raw_chars': 0, 'filtered_chars': 0,
            'error_msg': 'temporary browser outage',
            'saved_path': None, 'is_asset': False,
        })
        monkeypatch.setattr(
            handlers, '_finalize_tool_round', lambda *_a, **_k: None)

        task = _make_task()
        task['_userId'] = '41'
        task['config'] = {}
        round_entry = {}
        _tc_id, content, _is_read = handlers._handle_fetch_url(
            task, {}, 'fetch_url', 'tc-serial', {'url': url}, 1,
            round_entry, {}, '', False,
        )

        assert content.startswith('Failed to fetch')
        assert round_entry['_cacheableResult'] is False
