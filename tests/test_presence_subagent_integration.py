"""tests/test_presence_subagent_integration.py — presence fires through the
REAL SubAgent.run() path (not just direct _presence_* helper calls).

The unit tests prove each presence seam in isolation; this proves the wiring
fires end-to-end through the genuine swarm execution path:

  • Construct TWO real ``SubAgent`` instances sharing one parent ``convId`` +
    ``taskId`` + ``project_path``.
  • Stub the LLM at the dispatch boundary (``_default_dispatch_stream``) so each
    agent deterministically issues ONE ``write_file`` tool call editing the
    SAME file, then a final answer — no live model, no network.
  • Stub the tool executor so the "edit" doesn't touch disk but reports success
    (presence ``record_files`` fires on a successful file-edit tool).
  • Run both agents and assert a WITHIN-conversation conflict advisory was
    broadcast off the REAL ``lib.agent_core.push.hub`` with peer keys
    ``{conv#agent-1, conv#agent-2}``.

Negative control (in-test): a second pass that NEUTERS the announce seam in
``run()`` must NOT produce the advisory — proving the test depends on the real
seam firing, not on the helper being callable.

Offline + deterministic → safe for CI.
"""

from __future__ import annotations

import os
import sys
import threading

import pytest

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

pytestmark = pytest.mark.unit

SHARED_FILE = 'lib/llm/stream.py'


class _EditThenFinishLLM:
    """Round 1 → one write_file tool call on SHARED_FILE; round 2 → final answer.

    Mirrors the ``dispatch_stream`` contract used by SubAgent:
    ``(msg, stop_reason, usage)``. Per-agent call counter via the body's
    ``_task_id`` (== agent_id).

    Faithful to REAL swarm execution (the master runs sub-agents concurrently
    in a ThreadPoolExecutor): a ``threading.Barrier`` makes BOTH agents complete
    their round-1 file edit (and thus their presence ``record_files``) before
    EITHER proceeds to its final-answer round (which ends the run and marks the
    peer idle). So at the moment the second agent's ``record_files`` runs, the
    first is still ACTIVE — exactly the concurrent overlap the conflict detector
    must catch. Deterministic + offline.
    """

    def __init__(self, n_agents=2):
        self._rounds = {}  # agent_id -> count
        self._lock = threading.Lock()
        self._barrier = threading.Barrier(n_agents)

    def __call__(self, body, *, on_content=None, on_thinking=None,
                 abort_check=None, prefer_model='', log_prefix='', on_retry=None):
        agent_id = body.get('_task_id', '?')
        with self._lock:
            n = self._rounds.get(agent_id, 0)
            self._rounds[agent_id] = n + 1
        if n == 0:
            # First round: emit a tool call editing the shared file.
            if on_content:
                on_content('editing the shared file')
            msg = {
                'role': 'assistant',
                'content': '',
                'tool_calls': [{
                    'id': f'tc-{agent_id}',
                    'type': 'function',
                    'function': {
                        'name': 'write_file',
                        'arguments': f'{{"path": "{SHARED_FILE}", '
                                     f'"content": "x", "description": "edit"}}',
                    },
                }],
            }
            return msg, 'tool_calls', {'prompt_tokens': 5, 'completion_tokens': 3,
                                       'total_tokens': 8}
        # Before finishing (which idles this peer), wait until BOTH agents have
        # done their round-1 edit, so the overlap is detected while both active.
        if n == 1:
            try:
                self._barrier.wait(timeout=10)
            except threading.BrokenBarrierError:
                pass
        # Second round: final answer, no tool calls.
        if on_content:
            on_content('done')
        return ({'role': 'assistant', 'content': 'done'}, 'end_turn',
                {'prompt_tokens': 4, 'completion_tokens': 2, 'total_tokens': 6})


def _stub_tool_executor(monkeypatch):
    """Make write_file report success WITHOUT touching disk.

    SubAgent._dispatch_tool calls lib.tasks_pkg.executor._execute_tool_one;
    presence record_files fires on a *successful* file-edit tool, reading the
    path from the tool args (not from disk), so a success-reporting stub is
    enough to drive the seam.
    """
    import lib.tasks_pkg.executor as ex

    def _fake_exec(task_proxy, tool_call, fn_name, tc_id, fn_args,
                   round_num, round_entry, cfg, project_path, project_enabled):
        return (round_entry, f'(stub) {fn_name} ok', None)

    monkeypatch.setattr(ex, '_execute_tool_one', _fake_exec)


def _build_agent(agent_id_suffix, conv_id, task_id, project_path, llm):
    from lib.swarm.agent import SubAgent
    from lib.swarm.types import SubTaskSpec
    spec = SubTaskSpec(role='coder', objective='edit the shared file',
                       id=agent_id_suffix)
    return SubAgent(
        spec,
        parent_task={'id': task_id, 'convId': conv_id, '_userId': 17,
                     'config': {'convTitle': 'Swarm session'}},
        # write_file MUST be a KNOWN tool: SubAgent.run() now routes every tool
        # call through ingest_tool_call(known_tools=...) which REJECTS a name
        # absent from the agent's tool set as a hallucination — with all_tools=[]
        # the write_file edit was rejected, so record_files never fired and no
        # conflict was detected. Advertise it so the real edit path runs.
        all_tools=[{'type': 'function', 'function': {
            'name': 'write_file',
            'description': 'write a file',
            'parameters': {'type': 'object', 'properties': {
                'path': {'type': 'string'}, 'content': {'type': 'string'},
                'description': {'type': 'string'}},
                'required': ['path', 'content']}}}],
        model='mock-model',
        thinking_enabled=False,
        project_path=project_path,
        dispatch_stream_fn=llm,
    )


def _run_two_agents_capture(monkeypatch, tmp_path, *, break_announce=False):
    """Run two SubAgents on one conv+file; return captured presence frames."""
    _stub_tool_executor(monkeypatch)
    root = str(tmp_path / 'proj')
    conv_id = 'integ-conv'
    task_id = 'integ-task'

    # Reset the registry state for this root so prior tests don't bleed in.
    import lib.presence.registry as reg
    monkeypatch.setattr(reg, '_state', {})
    monkeypatch.setattr(reg, '_sweeper_started', True)  # don't spawn the timer

    if break_announce:
        # NEGATIVE CONTROL: neuter the announce seam so the sub-agent peers are
        # never registered → record_files finds no peer → no conflict advisory.
        from lib.swarm.agent import SubAgent
        monkeypatch.setattr(SubAgent, '_presence_announce',
                            lambda self, phase='working': None)

    from lib.agent_core.push import hub
    captured = []
    listener = lambda ch, tid, payload: captured.append({'ch': ch, **payload})  # noqa: E731
    hub.add_listener(listener)
    try:
        llm = _EditThenFinishLLM(n_agents=2)
        a1 = _build_agent('agent-1', conv_id, task_id, root, llm)
        a2 = _build_agent('agent-2', conv_id, task_id, root, llm)
        # Run CONCURRENTLY (as the real swarm does) — the LLM stub's barrier
        # holds both at the post-edit point so both peers are ACTIVE when the
        # overlap is detected. Deterministic despite the threads.
        threads = [threading.Thread(target=a.run) for a in (a1, a2)]
        for th in threads:
            th.start()
        for th in threads:
            th.join(timeout=15)
    finally:
        hub.remove_listener(listener)
    return captured


def test_two_subagents_real_run_emit_within_conv_conflict(monkeypatch, tmp_path):
    """The REAL run path fires announce + record_files → within-conv conflict."""
    captured = _run_two_agents_capture(monkeypatch, tmp_path)

    # Both sub-agents announced as distinct peers (composite key).
    updates = [f for f in captured if f.get('kind') == 'update']
    agent_ids = {(f.get('peer') or {}).get('agentId') for f in updates}
    assert 'agent-coder-agent-1' in agent_ids
    assert 'agent-coder-agent-2' in agent_ids

    # The decisive assertion: a within-conversation conflict advisory fired,
    # with the two DISTINCT sub-agent peer keys.
    conflicts = [f for f in captured if f.get('kind') == 'conflict']
    assert conflicts, 'expected a within-conversation conflict advisory'
    peers = set(conflicts[-1]['conflict']['peers'])
    assert peers == {'integ-conv#agent-coder-agent-1',
                     'integ-conv#agent-coder-agent-2'}, peers
    assert SHARED_FILE in conflicts[-1]['conflict']['message']
    assert all(f['ch'] == 'presence' for f in captured)


def test_negative_control_broken_announce_yields_no_conflict(monkeypatch, tmp_path):
    """If the announce seam in run() doesn't fire, no advisory appears.

    Proves the green test above depends on the REAL seam firing through
    run(), not merely on the _presence_* helpers being callable.
    """
    captured = _run_two_agents_capture(monkeypatch, tmp_path, break_announce=True)
    conflicts = [f for f in captured if f.get('kind') == 'conflict']
    assert conflicts == [], (
        'with the announce seam neutered there must be NO conflict advisory '
        f'(got {len(conflicts)})')


if __name__ == '__main__':
    import sys as _sys
    _sys.exit(pytest.main([__file__, '-v']))
