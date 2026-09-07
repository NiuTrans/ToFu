"""tests/test_flow_tool_round_rich_meta.py — Goal-mode todo_write keeps its rich card.

A flow (goal-mode) role node executes tools through the swarm SubAgent
substrate. The durable ``tool_log`` row used to keep only the prose preview,
so ``project_flow_tool_rounds`` built a bare fetch-shaped result meta and the
chat renderer fell through to the generic English receipt line ("Checklist:
4 steps (4 done) · 550 chars") instead of the localized checklist progress
card that a normal chat turn renders.

The fix plumbs the structured payload the executor already finalized
(``_handle_todo_write`` → ``_finalize_tool_round`` meta extras) onto the
durable row at dispatch time and merges it back at projection time:

  * ``swarm.agent.flow_structured_result_meta`` harvests exactly the flat
    display keys (todos / revision / badge / …) — never the heavy engine
    state (``todoState`` stack+history) or the prose fields;
  * ``project_flow_tool_rounds`` merges ``row['result_meta']`` into the
    projected round's first result meta, so the frontend's
    ``_renderTodoBlock`` (trigger: ``meta.todos``) fires and the revision
    collapser (``_projectTodoRoundsForDisplay``) gets its identity keys.

Negative controls pin the pre-fix shape for rows WITHOUT the payload so
normal/legacy flow rows stay byte-identical.

@pytest.mark.unit — pure in-process, no LLM, no IO.
"""

import pytest

pytestmark = pytest.mark.unit


def _todo_result_meta(**overrides):
    meta = {
        'todos': [
            {'id': 'a', 'content': 'First step', 'status': 'completed'},
            {'id': 'b', 'content': 'Second step', 'status': 'in_progress'},
        ],
        'todoOperation': 'sync',
        'todoNoop': False,
        'todoRejected': False,
        'todoAutoPopped': [],
        'todoUpdateCount': 2,
        'todoBreadcrumbs': [
            {'checklist_id': 'c1', 'label': 'Root task', 'revision': 2},
        ],
        'checklistId': 'c1',
        'todoRevision': 2,
        'badge': '1/2',
    }
    meta.update(overrides)
    return meta


def _todo_tool_log_row(**overrides):
    row = {
        'round': 1,
        'tool': 'todo_write',
        'args_brief': 'Checklist: 2 steps',
        'timestamp': 1_700_000_000,
        'preview': 'Checklist updated: 1/2 completed (revision 2, depth 1).',
        'preview_full_chars': 48,
        'result_meta': _todo_result_meta(),
    }
    row.update(overrides)
    return row


# ── projection merge ──────────────────────────────────────────────────

def test_projection_merges_structured_meta_into_result():
    from lib.orchestration_chat_flow_projection import project_flow_tool_rounds

    rounds = project_flow_tool_rounds([_todo_tool_log_row()])
    assert len(rounds) == 1
    entry = rounds[0]
    assert entry['toolName'] == 'todo_write'
    assert entry['status'] == 'done'
    meta = entry['results'][0]
    # The rich-card trigger and the revision-collapser identity keys.
    assert [t['content'] for t in meta['todos']] == ['First step', 'Second step']
    assert meta['todoRevision'] == 2
    assert meta['checklistId'] == 'c1'
    assert meta['todoOperation'] == 'sync'
    assert meta['todoUpdateCount'] == 2
    assert meta['badge'] == '1/2'
    # The base fetch-shaped fields survive the merge untouched.
    assert meta['source'] == 'Flow'
    assert meta['fetched'] is True
    assert meta['fetchedChars'] == 48


def test_projection_without_result_meta_keeps_prefix_shape():
    """Negative control: a row without the payload projects EXACTLY the
    pre-fix bare meta — normal/legacy flow rows are byte-identical."""
    from lib.orchestration_chat_flow_projection import project_flow_tool_rounds

    row = _todo_tool_log_row()
    row.pop('result_meta')
    rounds = project_flow_tool_rounds([row])
    meta = rounds[0]['results'][0]
    assert set(meta) == {
        'toolName', 'title', 'snippet', 'source', 'fetched', 'fetchedChars',
    }


def test_projection_empty_results_branch_still_surfaces_meta():
    """A cleared checklist can leave no preview chars: the meta must still
    reach the renderer (its trigger is meta.todos, not the fetch fields)."""
    from lib.orchestration_chat_flow_projection import project_flow_tool_rounds

    row = _todo_tool_log_row(preview='', preview_full_chars=0)
    rounds = project_flow_tool_rounds([row])
    meta = rounds[0]['results'][0]
    assert meta['todos']
    assert meta['source'] == 'Flow'
    assert meta['fetched'] is False


def test_projection_ignores_malformed_result_meta():
    from lib.orchestration_chat_flow_projection import project_flow_tool_rounds

    for bad in ('not-a-mapping', 42, []):
        row = _todo_tool_log_row(result_meta=bad)
        rounds = project_flow_tool_rounds([row])
        meta = rounds[0]['results'][0]
        assert 'todos' not in meta, bad


# ── agent identity ────────────────────────────────────────────────────

def test_projection_carries_agent_identity_for_inspector_stream():
    """``agentId`` on the durable row must survive projection — the debug
    entry re-derives the ``{parent}#agent:{agentId}`` Request Inspector
    stream from the settled round."""
    from lib.orchestration_chat_flow_projection import project_flow_tool_rounds

    rounds = project_flow_tool_rounds([_todo_tool_log_row(agentId='agent-9')])
    assert rounds[0]['agentId'] == 'agent-9'


def test_projection_without_agent_id_omits_the_key():
    """Negative control: ordinary flow rows stay byte-identical (no empty
    agentId key leaking into the wire shape)."""
    from lib.orchestration_chat_flow_projection import project_flow_tool_rounds

    rounds = project_flow_tool_rounds([_todo_tool_log_row()])
    assert 'agentId' not in rounds[0]


# ── dispatch-time harvest ─────────────────────────────────────────────

def test_harvest_extracts_only_declared_display_keys():
    from lib.swarm.agent import flow_structured_result_meta

    results = [{
        'toolName': 'todo_write',
        'title': 'Checklist · 1/2 done',
        'snippet': 'Checklist updated: 1/2 completed …',
        'source': 'Checklist',
        # Heavy engine state must NOT ride the durable row.
        'todoState': {'stack': [{'todos': [], 'history': [{}] * 40}]},
        'todoRejectReason': '',
        'rootCompleted': False,
        **_todo_result_meta(),
    }]
    harvested = flow_structured_result_meta('todo_write', results)
    assert harvested['todos'][0]['id'] == 'a'
    assert harvested['todoRevision'] == 2
    assert harvested['todoUpdateCount'] == 2
    assert harvested['badge'] == '1/2'
    for excluded in ('todoState', 'todoRejectReason', 'rootCompleted',
                     'snippet', 'title', 'source', 'toolName'):
        assert excluded not in harvested, excluded


def test_harvest_noops_for_other_tools_and_malformed_input():
    from lib.swarm.agent import flow_structured_result_meta

    assert flow_structured_result_meta('write_file', [{'todos': []}]) == {}
    assert flow_structured_result_meta('todo_write', None) == {}
    assert flow_structured_result_meta('todo_write', []) == {}
    assert flow_structured_result_meta('todo_write', 'nope') == {}
    assert flow_structured_result_meta('todo_write', ['not-a-dict']) == {}
    # Missing keys harvest nothing but do not fail.
    assert flow_structured_result_meta('todo_write', [{'badge': '0/1'}]) == {
        'badge': '0/1'}


def test_harvest_run_command_exit_card_keys():
    """The settled command card reads its ``$`` line and exit pill from flat
    results[0] keys; a goal-mode shell round must carry them. ``output``
    stays out — the prose preview already carries it and it is the heavy
    field the checkpoint budget reclaims first."""
    from lib.swarm.agent import flow_structured_result_meta

    harvested = flow_structured_result_meta('run_command', [{
        'toolName': 'code_exec',
        'command': 'npm run build',
        'output': '...12k chars of build log...',
        'exitCode': 0,
        'timedOut': False,
    }])
    assert harvested == {'command': 'npm run build', 'exitCode': 0,
                         'timedOut': False}


def test_harvest_run_command_terminal_badges():
    from lib.swarm.agent import flow_structured_result_meta

    harvested = flow_structured_result_meta('run_command', [{
        'toolName': 'code_exec', 'command': 'make',
        'exitCode': 'not-run', 'notRun': True, 'timedOut': False,
        'badge': 'precheck failed', 'reason': 'denied',
        'interrupted': True, 'recovered': True,
        'grepSearchIntercepted': True,
    }])
    assert harvested['notRun'] is True
    assert harvested['badge'] == 'precheck failed'
    assert harvested['reason'] == 'denied'
    assert harvested['interrupted'] is True
    assert harvested['grepSearchIntercepted'] is True
    assert 'output' not in harvested


def test_projection_run_command_card_keys_render_settled_command():
    """Without the harvested meta the settled card degrades to a bare ``$``
    and an unknown-exit pill (the pre-fix goal-mode rendering)."""
    from lib.orchestration_chat_flow_projection import project_flow_tool_rounds

    row = {
        'round': 3, 'tool': 'run_command',
        'args_brief': 'npm run build',
        'timestamp': 1_700_000_000,
        'preview': '$ npm run build\n…\n[exit code: 0]',
        'preview_full_chars': 34,
        'result_meta': {'command': 'npm run build', 'exitCode': 0,
                        'timedOut': False},
    }
    rounds = project_flow_tool_rounds([row])
    meta = rounds[0]['results'][0]
    assert meta['command'] == 'npm run build'
    assert meta['exitCode'] == 0
    assert meta['timedOut'] is False


def test_dispatch_records_edited_path_on_tool_log_row():
    """The settled file-changes block derives from per-row edit markers;
    without the durable stamp a goal-mode turn could never render it."""
    import inspect
    import lib.swarm.agent as agent_mod

    single_src = inspect.getsource(agent_mod.SubAgent._execute_single_tool)
    assert "tool_log_row['edited_path'] = _edited" in single_src
    assert "tool_log_row['edited_action']" in single_src


def test_dispatch_tool_persists_result_meta_onto_tool_log_row():
    """End of the capture seam: _execute_single_tool hands _dispatch_tool a
    sink and writes the harvested payload onto the durable tool_log row."""
    import inspect
    import lib.swarm.agent as agent_mod

    dispatch_src = inspect.getsource(agent_mod.SubAgent._dispatch_tool)
    assert 'meta_sink' in dispatch_src
    assert 'flow_structured_result_meta' in dispatch_src
    single_src = inspect.getsource(agent_mod.SubAgent._execute_single_tool)
    assert "tool_log_row['result_meta'] = meta_sink" in single_src
    # Bounded checkpoint: the historical-row compaction reclaims prose bodies
    # and heavy meta lists, but keeps args_brief (the settled card's command
    # line) and the flat meta scalars (badge / exitCode / counters).
    assert "historical['args_brief'] = ''" not in single_src
    assert "historical.pop('result_meta', None)" not in single_src
    assert "old_meta = historical.get('result_meta')" in single_src
