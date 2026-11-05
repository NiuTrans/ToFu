"""Regression: fetch_url keeps one canonical, runtime-independent schema."""
from __future__ import annotations

import inspect
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import lib.tools.search as search_tools  # noqa: E402


@pytest.mark.unit
class TestFetchUrlToolSchema:

    def test_per_request_consumers_use_the_builder(self):
        # Source pin: the three per-request consumers must call the builder,
        # not the import-time FETCH_URL_TOOL snapshot (which stays for the
        # static capability listing only).
        import lib.paper.tools as paper_tools
        import lib.scheduler.timer._poll as timer_poll
        import lib.tools.registry._build as registry_build

        fetch_builder = inspect.getsource(registry_build._build_fetch)
        assert 'build_fetch_url_tool()' in fetch_builder
        assert 'FETCH_URL_TOOL' not in fetch_builder

        paper_builder = inspect.getsource(
            paper_tools.build_research_tool_schemas)
        assert 'build_fetch_url_tool()' in paper_builder
        assert 'FETCH_URL_TOOL' not in paper_builder

        poll_src = inspect.getsource(timer_poll._build_poll_tools)
        assert 'build_fetch_url_tool()' in poll_src
        assert 'FETCH_URL_TOOL' not in poll_src

    def test_schema_is_semantic(self):
        schema = search_tools.build_fetch_url_tool()
        wire = json.dumps(schema, ensure_ascii=False, sort_keys=True)
        for guidance in (
            'HTTP(S)', 'file://', 'read_files', 'server-staging',
            'selected browser', 'browser Downloads', 'authorized filesystem',
            'verify receipt', 'Page Links', 'Concurrent batch',
        ):
            assert guidance in wire
        # Tofu runs no relevance gate on fetched pages, so the model is not
        # asked for a relevance goal.
        assert 'gate' not in wire.lower()
        assert set(schema['function']['parameters']['properties']) == {'url', 'urls', 'extract'}

    def test_schema_bytes_are_stable_across_builds(self):
        first = json.dumps(search_tools.build_fetch_url_tool(), sort_keys=True,
                           separators=(',', ':'))
        second = json.dumps(search_tools.build_fetch_url_tool(), sort_keys=True,
                            separators=(',', ':'))
        assert first == second
