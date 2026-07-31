#!/usr/bin/env python3
"""tests/test_tool_args_brief.py — name-keyed args brief for swarm/timer tool lines.

ROOT-CAUSE fix verification. The swarm panel's per-call line and the timer
poll's toolTrace both used ``str(fn_args)[:N]`` — a raw dict-repr truncation.
For ``write_file``/``apply_diff`` the model usually emits ``content``/``search``
FIRST, so the path never fit inside the 200/120-char budget (the panel showed
``{'description': '…', 'content': '# xxxx…`` — "can't tell which file is being
read/written"); a ``read_files`` batch rendered as ``{'reads': [{'path': …``
noise. Reload recovery replays the same ``args_brief`` from ``tool_log``
(lib/swarm/master.py ``_snapshot_tool_timeline``), so live + recovery shared
one root.

The fix routes both surfaces through ``lib.project_mod.format_tool_args_brief``:
  • project tools  → ``project_tool_display`` (path-first labels)
  • web_search     → the query (batch: ``N searches: q1; q2…``)
  • fetch_url      → the URL   (batch: ``N URLs: u1; u2…``)
  • run_command    → the command
  • unknown tools  → truncated repr fallback (old behaviour, bounded)

Run standalone:  python3 tests/test_tool_args_brief.py
or via pytest.
"""

from __future__ import annotations

import json
import os
import sys

import pytest

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from lib.project_mod import format_tool_args_brief

pytestmark = pytest.mark.unit


# A write_file call whose argument ORDER buries the path behind a >200-char
# content blob — the exact production shape that broke the panel. Python dicts
# preserve the model's emission order, and models typically emit description /
# content before path.
_WRITE_FILE_ARGS = {
    'description': 'add retry logic',
    'content': '# ' + ('x' * 400),
    'path': 'lib/llm/_transport.py',
}


def test_write_file_brief_shows_path_despite_content_first_ordering():
    brief = format_tool_args_brief('write_file', _WRITE_FILE_ARGS)
    assert brief.startswith('Write ')
    assert 'lib/llm/_transport.py' in brief
    # The old raw-repr behaviour provably cannot satisfy this (see NEUTER below).
    assert 'lib/llm/_transport.py' not in str(_WRITE_FILE_ARGS)[:200]


def test_apply_diff_brief_shows_path_despite_search_first_ordering():
    args = {'search': 'def old():\n' + ('y' * 300), 'replace': 'def new():',
            'path': 'lib/a.py', 'description': 'rename'}
    brief = format_tool_args_brief('apply_diff', args)
    assert 'lib/a.py' in brief
    assert 'lib/a.py' not in str(args)[:200]


def test_read_files_batch_lists_each_path():
    args = {'reads': [{'path': f'src/f{i}.py'} for i in range(6)]}
    brief = format_tool_args_brief('read_files', args)
    assert brief.startswith('Read 6 files:')
    for i in range(4):  # the first four are rendered verbatim
        assert f'f{i}.py' in brief
    assert '+2 more' in brief
    # None of the raw-repr noise survives.
    assert "'reads'" not in brief and "{'path'" not in brief


def test_read_files_raw_json_string_coerced():
    """Timer polls hand the formatter the RAW arguments string — it must be
    parsed, not repr-truncated into ``{"reads": [{"path": …`` noise."""
    raw = json.dumps({'reads': [{'path': 'a.py'}, {'path': 'b.py'}]})
    assert format_tool_args_brief('read_files', raw) == 'Read 2 files: a.py; b.py'


def test_web_search_query_and_batch():
    assert format_tool_args_brief('web_search', {'query': 'q1'}) == 'q1'
    brief = format_tool_args_brief(
        'web_search', {'queries': [{'query': 'q1'}, {'query': 'q2'}]})
    assert brief == '2 searches: q1; q2'


def test_fetch_url_url_and_batch():
    assert format_tool_args_brief(
        'fetch_url', {'url': 'https://x.com/a'}) == 'https://x.com/a'
    brief = format_tool_args_brief(
        'fetch_url', {'urls': [{'url': 'https://x.com/a'}, {'url': 'https://y.com/b'}]})
    assert brief == '2 URLs: https://x.com/a; https://y.com/b'


def test_run_command_brief_is_command():
    assert format_tool_args_brief(
        'run_command', {'command': 'pytest -x'}) == 'pytest -x'


def test_unknown_tool_falls_back_to_repr():
    args = {'foo': 'bar'}
    assert format_tool_args_brief('some_future_tool', args) == str(args)


def test_max_len_enforced_with_ellipsis():
    brief = format_tool_args_brief('run_command', {'command': 'c' * 500}, max_len=120)
    assert len(brief) == 120
    assert brief.endswith('…')


def test_non_dict_args_pass_through_bounded():
    assert format_tool_args_brief('write_file', 'not json at all') == 'not json at all'
    assert format_tool_args_brief('write_file', '') == ''
    long_raw = 'z' * 500
    assert len(format_tool_args_brief('write_file', long_raw, max_len=120)) == 120


# ─────────────────────────────────────────────────────────────────────────────
#  Wiring: swarm SubAgent._execute_single_tool must drive BOTH the persisted
#  tool_log AND the live SSE event from ONE format_tool_args_brief call
#  (reload recovery replays tool_log — two independent formatters could drift).
# ─────────────────────────────────────────────────────────────────────────────

def _make_agent(on_event):
    from lib.swarm.agent import SubAgent
    from lib.swarm.protocol import SubTaskSpec
    spec = SubTaskSpec(id='wb', role='general', objective='wiring probe')
    return SubAgent(
        spec,
        parent_task={'id': 't-wiring', 'convId': ''},
        all_tools=[], model='', thinking_enabled=False,
        on_event=on_event, abort_check=None,
        project_path='', artifact_store=None,
    )


def test_swarm_agent_tool_log_and_sse_share_one_formatter(monkeypatch):
    import lib.swarm.agent as agent_mod
    calls = []
    monkeypatch.setattr(
        agent_mod, 'format_tool_args_brief',
        lambda name, args, **kw: calls.append(name) or f'BRIEF::{name}')

    events = []
    agent = _make_agent(events.append)
    # read_artifact is handled locally and, with no store wired, returns a
    # clean error string — the formatter + log + SSE all run BEFORE dispatch.
    result = agent._execute_single_tool(
        {'id': 'c1', 'function': {'name': 'read_artifact',
                                  'arguments': json.dumps({'key': 'missing'})}},
        round_num=1)

    assert result == 'Error: artifact store not available'
    assert calls == ['read_artifact'], (
        'formatter must be consulted exactly once per tool call')
    # Persisted log carries the formatted brief …
    assert agent.result.tool_log[0]['args_brief'] == 'BRIEF::read_artifact'
    # … and the LIVE SSE rows (start + finish) carry the identical string.
    briefs = [e['argsBrief'] for e in events
              if e.get('type') == 'swarm_agent_tool_call']
    assert briefs and set(briefs) == {'BRIEF::read_artifact'}


# ─────────────────────────────────────────────────────────────────────────────
#  NEUTER proof: the pre-fix behaviour cannot satisfy the new contract — so
#  the assertions above only pass while the name-keyed formatter is wired.
# ─────────────────────────────────────────────────────────────────────────────

def test_neuter_legacy_repr_truncation_cannot_satisfy_contract():
    legacy = str(_WRITE_FILE_ARGS)[:200]           # the pre-fix code path
    assert 'lib/llm/_transport.py' not in legacy   # ← the reported bug
    fixed = format_tool_args_brief('write_file', _WRITE_FILE_ARGS)
    assert 'lib/llm/_transport.py' in fixed        # ← the fix


if __name__ == '__main__':
    pytest.main([__file__, '-v', '-s'])
