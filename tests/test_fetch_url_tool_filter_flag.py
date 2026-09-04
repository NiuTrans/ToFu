"""Regression: the fetch_url tool schema must follow the RUNTIME
``lib.LLM_CONTENT_FILTER_ENABLED`` flag (Settings toggle, hot-applied by
routes/config.py), not the ``FETCH_LLM_FILTER`` env var that only seeds the
flag's default at import time.

Pre-fix, ``lib/tools/search.py`` read the env var directly, so after a
Settings toggle the schema kept advertising (or hiding) the ``reason``
relevance-gate parameter stale — and the per-request consumers used the
import-time ``FETCH_URL_TOOL`` snapshot, so even a correct source would not
have reached the model until restart.

Pure-pytest: monkeypatch the flag on the already-imported ``lib`` package and
rebuild the schema per call — no server needed.
"""
from __future__ import annotations

import inspect
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import lib as _lib  # noqa: E402
import lib.tools.search as search_tools  # noqa: E402


def _reason_param_present(monkeypatch, *, flag, env):
    monkeypatch.setattr(_lib, 'LLM_CONTENT_FILTER_ENABLED', flag, raising=False)
    if env is None:
        monkeypatch.delenv('FETCH_LLM_FILTER', raising=False)
    else:
        monkeypatch.setenv('FETCH_LLM_FILTER', env)
    schema = search_tools.build_fetch_url_tool()
    props = schema['function']['parameters']['properties']
    return 'reason' in props, schema['function']['description']


@pytest.mark.unit
class TestFetchUrlToolFilterFlag:

    def test_flag_on_exposes_reason(self, monkeypatch):
        present, description = _reason_param_present(monkeypatch, flag=True, env=None)
        assert present is True
        assert 'relevance GATE' in description

    def test_flag_off_hides_reason(self, monkeypatch):
        present, description = _reason_param_present(monkeypatch, flag=False, env=None)
        assert present is False
        assert 'relevance GATE' not in description

    def test_runtime_flag_wins_over_env_on(self, monkeypatch):
        # env says ON but the Settings toggle turned the filter OFF → hide.
        present, _ = _reason_param_present(monkeypatch, flag=False, env='1')
        assert present is False

    def test_runtime_flag_wins_over_env_off(self, monkeypatch):
        # env says OFF but the Settings toggle turned the filter ON → expose.
        present, _ = _reason_param_present(monkeypatch, flag=True, env='0')
        assert present is True

    def test_search_module_no_longer_reads_env(self):
        # Source pin: the env var must not be READ anywhere in the schema
        # path (the docstring may still mention its name for context).
        src = inspect.getsource(search_tools)
        assert "environ.get('FETCH_LLM_FILTER" not in src
        assert 'environ.get("FETCH_LLM_FILTER' not in src

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

    @pytest.mark.parametrize(('flag', 'budget'), ((True, 400), (False, 300)))
    def test_runtime_schema_is_semantic_and_bounded(
            self, monkeypatch, flag, budget):
        from lib.tools.gateway import tool_schema_tokens

        monkeypatch.setattr(
            _lib, 'LLM_CONTENT_FILTER_ENABLED', flag, raising=False)
        schema = search_tools.build_fetch_url_tool()
        wire = json.dumps(schema, ensure_ascii=False, sort_keys=True)
        for guidance in (
            'HTTP(S)', 'file://', 'read_files', 'server staging',
            'selected browser', 'browser Downloads', 'authorized filesystem',
            'verify its own receipt', 'Page Links', 'Concurrent batch',
        ):
            assert guidance in wire
        if flag:
            for guidance in (
                'relevance GATE', 'Failed to fetch',
                'does not select passages or summarize', 'batches bypass',
            ):
                assert guidance in wire
        else:
            assert 'relevance GATE' not in wire
        assert tool_schema_tokens([schema], model='kimi-k3') <= budget
