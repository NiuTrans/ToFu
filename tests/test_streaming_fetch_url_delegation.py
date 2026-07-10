"""Regression: streaming pre-exec ``fetch_url`` delegates to the authoritative
``handlers.search._fetch_url_one`` — so binary file assets and text assets are
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
        with patch('lib.tasks_pkg.handlers.search._fetch_url_one',
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
        with patch('lib.tasks_pkg.handlers.search._fetch_url_one', return_value=fail):
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

        with patch('lib.tasks_pkg.handlers.search._fetch_url_one', side_effect=fake) as m:
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
        with patch('lib.tasks_pkg.handlers.search._fetch_url_one', return_value=neutered):
            out = acc._execute_one('fetch_url', {'url': url})
        assert 'file asset' not in out
        assert out.startswith('Failed to fetch')
