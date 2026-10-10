"""tests/scheduler/test_direct_dispatch_tool_alias.py — cross-harness tool-name aliasing
on the DIRECT-executor dispatch paths (swarm sub-agent + timer poll).

Root cause (2026-07-01): the tool-NAME alias layer (``resolve_tool_name`` in
``lib/tool_input_repair.py``, e.g. ``WebFetch``→``fetch_url``, ``Read``→
``read_files``, ``Bash``→``run_command``) ran ONLY inside the main chat
dispatcher's ``parse_tool_calls``. But two paths call ``_execute_tool_one``
DIRECTLY, bypassing it entirely:

  * swarm sub-agents — ``lib/swarm/agent.py::_execute_single_tool`` → ``_dispatch_tool``
  * timer polls      — ``lib/scheduler/timer.py::_execute_poll_tool``

So a researcher sub-agent (the heavy fetch user) emitting Claude Code's
``WebFetch`` hit the executor's hard ``Error: unknown tool "WebFetch"`` wall
and burned a round — the exact "我们的 alias 护栏没有生效" symptom. The fix
applies ``resolve_tool_name`` at BOTH bypass sites before dispatch.

These tests drive the REAL dispatch functions with a spy ``_execute_tool_one``
that captures the ``fn_name`` / ``fn_args`` it actually receives, and assert
the wrong-harness name arrives RESOLVED. The negative control (comment out
either alias block) makes the corresponding assertion fail with the raw
un-aliased name.

2026-07-01 (unified seam): both paths now funnel through
``lib.tool_input_repair.ingest_tool_call`` — one front door doing name-drop
guard → name-alias → JSON decode+repair → schema/param repair → hallucination
reject. So these tests additionally assert the schema/param repair (previously
MISSING on the direct paths) and the swarm rejection of an invented name.
"""

import json
import uuid

import pytest

pytestmark = pytest.mark.unit


def _tc(name: str, args: str = '{}'):
    return {'id': 'tc_' + uuid.uuid4().hex[:8],
            'function': {'name': name, 'arguments': args}}


# ══════════════════════════════════════════
#  Timer poll path
# ══════════════════════════════════════════

def test_timer_poll_aliases_webfetch_to_fetch_url(monkeypatch):
    """A timer poll calling `WebFetch` must reach the executor as `fetch_url`."""
    import lib.scheduler.timer as timer_mod

    captured = {}

    def _spy(task, tc, fn_name, tc_id, fn_args, rn,
             round_entry, cfg, project_path, project_enabled):
        captured['fn_name'] = fn_name
        return (None, 'ok', None)

    import lib.tasks_pkg.executor as _ex
    monkeypatch.setattr(_ex, '_execute_tool_one', _spy, raising=True)

    timer_mod._execute_poll_tool(
        _tc('WebFetch', '{"url": "https://arxiv.org/abs/2605.12882"}'),
        'tmr_' + uuid.uuid4().hex[:8], project_path='', owner_user_id=1)

    assert captured['fn_name'] == 'fetch_url', (
        'timer poll must alias the Claude-Code WebFetch name to fetch_url '
        'before dispatch — got %r' % captured.get('fn_name'))


@pytest.mark.parametrize('wrong,canonical', [
    ('Read', 'read_files'),
    ('Grep', 'grep_search'),
    ('Bash', 'run_command'),
])
def test_timer_poll_aliases_common_claude_code_names(monkeypatch, wrong, canonical):
    import lib.scheduler.timer as timer_mod

    captured = {}

    def _spy(task, tc, fn_name, tc_id, fn_args, rn,
             round_entry, cfg, project_path, project_enabled):
        captured['fn_name'] = fn_name
        return (None, 'ok', None)

    import lib.tasks_pkg.executor as _ex
    monkeypatch.setattr(_ex, '_execute_tool_one', _spy, raising=True)

    timer_mod._execute_poll_tool(_tc(wrong), 'tmr_' + uuid.uuid4().hex[:8],
                                 project_path='', owner_user_id=1)
    assert captured['fn_name'] == canonical


def test_timer_poll_unknown_tool_passes_through_unchanged(monkeypatch):
    """An un-aliasable invented name must NOT be silently rewritten."""
    import lib.scheduler.timer as timer_mod

    captured = {}

    def _spy(task, tc, fn_name, tc_id, fn_args, rn,
             round_entry, cfg, project_path, project_enabled):
        captured['fn_name'] = fn_name
        return (None, 'ok', None)

    import lib.tasks_pkg.executor as _ex
    monkeypatch.setattr(_ex, '_execute_tool_one', _spy, raising=True)

    timer_mod._execute_poll_tool(_tc('totally_made_up_xyz'),
                                 'tmr_' + uuid.uuid4().hex[:8], project_path='',
                                 owner_user_id=1)
    assert captured['fn_name'] == 'totally_made_up_xyz'


# ══════════════════════════════════════════
#  Swarm sub-agent path
# ══════════════════════════════════════════

def _make_agent():
    """Build a SubAgent with a fetch-capable scoped tool set, no LLM needed."""
    from lib.swarm.agent import SubAgent
    from lib.swarm.protocol import SubTaskSpec

    tools = [
        {'type': 'function', 'function': {'name': 'fetch_url', 'parameters': {}}},
        {'type': 'function', 'function': {'name': 'web_search', 'parameters': {}}},
        {'type': 'function', 'function': {'name': 'read_files', 'parameters': {}}},
        {'type': 'function', 'function': {'name': 'grep_search', 'parameters': {}}},
        {'type': 'function', 'function': {'name': 'run_command', 'parameters': {}}},
    ]
    spec = SubTaskSpec(role='researcher', objective='fetch a page')
    return SubAgent(spec, parent_task={'id': 't1', 'convId': 'c1'},
                    all_tools=tools, thinking_enabled=False)












if __name__ == '__main__':
    import sys
    sys.exit(pytest.main([__file__, '-v']))
