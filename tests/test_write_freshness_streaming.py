"""Cached-read freshness validation tests (Fix B of pt_26c703c50a7c43ca).

Production forensics (2026-07-25, logs/app.log): the streaming pre-exec +
dedup-hit path serves read_files results WITHOUT going through
``_handle_project_tool`` — so a cached read can be arbitrarily older than
the disk, serving it hands the model stale bytes, and the freshness token
is never re-stamped, which made the write gate refuse forever after any
external edit (the 'stable file, 3× refused' loop — tasks db77d231 /
95c11cfa / 62c63e55 / 4204f7fa on static/styles.css and JOURNAL.md).

The two halves of the loop closure landed as sibling-coordinated commits:

  A. streaming pre-exec re-stamps the token after read_files
     (lib/tasks_pkg/streaming_tool_executor.py + the two streaming tests
     in test_write_freshness_handler.py) — OWNED BY SIBLING
     (mrzyh2g7zqg5iu, refusal-card epic); NOT duplicated here.
  B. THIS SUITE: the dedup/prefetch HIT for file-read tools is
     freshness-validated via ``cached_read_is_stale`` — a cached read
     whose covered file moved on disk is DROPPED and re-executed for real
     (fresh bytes + fresh token) instead of feeding the model stale
     content it can never write from
     (lib/tasks_pkg/tool_dispatch/_pipeline.py).
"""
from __future__ import annotations

import os
import threading

import pytest


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    """Fresh token store for every test."""
    monkeypatch.delenv('TOFU_WRITE_FRESHNESS_GATE', raising=False)
    from lib import write_freshness
    write_freshness._reset_for_tests()
    yield
    write_freshness._reset_for_tests()


@pytest.fixture
def workspace(tmp_path):
    proj = tmp_path / 'proj'
    proj.mkdir()
    (proj / 'a.py').write_text('def foo():\n    return 1\n')
    return str(proj)


def _task(conv_id):
    return {'id': f'task-{conv_id}', 'convId': conv_id,
            'messages': [], 'toolRounds': [],
            'events_lock': threading.Lock(), 'events': []}


def _write_gate(task, project_path, rel):
    """None when the write gate allows, refusal string when it refuses."""
    from lib.tasks_pkg.handlers._write_freshness_gate import check_write_freshness
    return check_write_freshness(
        task, 'write_file', {'path': rel, 'content': 'x'}, project_path)


@pytest.mark.unit
def test_cached_read_is_stale_helper(workspace, _isolate):
    """Unit: True exactly when a covered file moved since the conv's token;
    fail-open (False) with no token / fresh token / vanished file."""
    from lib.tasks_pkg.handlers._write_freshness_gate import (
        cached_read_is_stale, record_read_paths,
    )
    a = _task('convA')
    # No token at all → fail-open False.
    assert cached_read_is_stale(a, {'path': 'a.py'}, workspace) is False
    record_read_paths(a, {'path': 'a.py'}, workspace, 'v1')
    assert cached_read_is_stale(a, {'path': 'a.py'}, workspace) is False
    with open(os.path.join(workspace, 'a.py'), 'w', encoding='utf-8') as f:
        f.write('def foo():\n    return 2  # B\n')
    assert cached_read_is_stale(a, {'path': 'a.py'}, workspace) is True
    # Batch shape: re-record clears it again.
    record_read_paths(a, {'reads': [{'path': 'a.py'}]}, workspace, 'v2')
    assert cached_read_is_stale(
        a, {'reads': [{'path': 'a.py'}]}, workspace) is False


def _drive_pipeline(task, project_path, fn_args):
    """Run the REAL execute_tool_pipeline for one read_files call.

    ``_finalize_tool_round`` is the documented monkeypatch target (same
    discipline as test_write_freshness_handler.py) — replaced with a
    recorder so no manager/DB/WebSocket infra is needed; the dedup branch,
    the freshness validation and the real re-execution stay fully real.
    Returns the tool-result message contents appended to ``messages``.
    """
    import lib.tasks_pkg.tool_dispatch._pipeline as pl
    monkey_finalized = []
    orig = pl._finalize_tool_round
    pl._finalize_tool_round = lambda t, rn, re_, results, **kw: \
        monkey_finalized.append(results)
    try:
        parsed = [({'id': 'tc-1'}, 'read_files', 'tc-1', fn_args, 1,
                   {'query': 'read_files', 'toolCallId': 'tc-1'}, None)]
        messages: list = []
        pl.execute_tool_pipeline(task, parsed, {}, project_path, True,
                                 None, messages, [], 1, 'test-model')
    finally:
        pl._finalize_tool_round = orig
    return [str(m.get('content')) for m in messages if isinstance(m, dict)]


def _plant_cache(task, fn_args, content):
    from lib.tasks_pkg.tool_dispatch._flags import _make_cache_key
    task['_tool_result_cache'] = {}
    ck = _make_cache_key('read_files', fn_args)
    task['_tool_result_cache'][ck] = (content, False, 'dedup', None, None, None)
    return ck


@pytest.mark.unit
def test_pipeline_drops_stale_cached_read(workspace, _isolate):
    """A dedup-cached read whose file changed on disk must NOT be served:
    the pipeline drops it, re-executes for real (model sees v2), and the
    real read re-stamps the token so a follow-up write is allowed —
    the recovery loop the production refusal message instructs."""
    from lib.tasks_pkg.handlers._write_freshness_gate import record_read_paths
    a = _task('convA')
    record_read_paths(a, {'path': 'a.py'}, workspace, 'v1')
    _plant_cache(a, {'path': 'a.py'}, 'STALE-CACHED-v1')
    with open(os.path.join(workspace, 'a.py'), 'w', encoding='utf-8') as f:
        f.write('def foo():\n    return 2  # B\n')
    served = _drive_pipeline(a, workspace, {'path': 'a.py'})
    assert served and all('STALE-CACHED-v1' not in c for c in served), served
    assert any('return 2  # B' in c for c in served), served
    assert _write_gate(a, workspace, 'a.py') is None  # token re-stamped


@pytest.mark.unit
def test_pipeline_serves_fresh_cached_read(workspace, _isolate):
    """The perf path is preserved: when no covered file moved, the cached
    read IS served (no re-execution)."""
    from lib.tasks_pkg.handlers._write_freshness_gate import record_read_paths
    a = _task('convA')
    record_read_paths(a, {'path': 'a.py'}, workspace, 'current')
    _plant_cache(a, {'path': 'a.py'}, 'CACHED-OK-fresh')
    served = _drive_pipeline(a, workspace, {'path': 'a.py'})
    assert served and any('CACHED-OK-fresh' in c for c in served), served


@pytest.mark.unit
def test_neuter_stale_check_serves_stale(workspace, _isolate, monkeypatch):
    """NEUTER: amputate the cached-read staleness check → the pipeline
    serves the stale bytes again AND the token stays stale (the follow-up
    write is refused). Proves the validation is what bypasses the cache."""
    import lib.tasks_pkg.handlers._write_freshness_gate as gate
    from lib.tasks_pkg.handlers._write_freshness_gate import record_read_paths
    monkeypatch.setattr(gate, 'cached_read_is_stale', lambda *a, **k: False)
    a = _task('convA')
    record_read_paths(a, {'path': 'a.py'}, workspace, 'v1')
    _plant_cache(a, {'path': 'a.py'}, 'STALE-CACHED-v1')
    with open(os.path.join(workspace, 'a.py'), 'w', encoding='utf-8') as f:
        f.write('def foo():\n    return 2  # B\n')
    served = _drive_pipeline(a, workspace, {'path': 'a.py'})
    assert served and any('STALE-CACHED-v1' in c for c in served), served
    assert _write_gate(a, workspace, 'a.py') is not None  # still refused
