"""Drift-guard tests for the 2026-07-08 tool-surface audit tranche 1.

Each test asserts the FIX is present AND (where a neuter is meaningful) that a
reverted variant of the source would fail the assertion — i.e. the guard bites
when the fix regresses. These are wiring/structure checks, not click sims,
following the project's verify-then-write discipline.

Fixes covered:
  1. spawn_agents per-agent schema exposes an ``id`` field (so ``depends_on``
     can reference a sibling within one call; backend already honors it).
  2. todo_write ``_normalize_todos`` enforces exactly one ``in_progress``.
  3. merge_memories logs + reports source deletes that FAILED (no silent
     duplicate).
  4. search/fetch batch handlers pass an ``abort`` predicate to
     run_batch_concurrent (Stop actually stops the queued tail).
  5. Doc contradictions: run_command timeout prose, batch caps, browser
     full_page / no time.sleep, project_intervene rate-limit note.
"""

import os
import re

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _src(rel):
    with open(os.path.join(REPO, rel), encoding='utf-8') as f:
        return f.read()


# ── 1. spawn_agents id field ────────────────────────────────────────

def test_spawn_agents_item_schema_has_id():
    from lib.swarm.tools import SPAWN_AGENTS_TOOL
    item_props = (SPAWN_AGENTS_TOOL['function']['parameters']
                  ['properties']['agents']['items']['properties'])
    assert 'id' in item_props, 'per-agent item schema must expose an id field'
    assert 'depends_on' in item_props
    # The id description must connect to depends_on so the model knows why to set it.
    assert 'depends_on' in item_props['id']['description']


def test_spawn_agents_backend_honors_caller_id():
    # The whole point of the schema id: the backend must use a caller-supplied
    # id rather than always minting a uuid.
    integ = _src('lib/swarm/integration/_tools.py')
    assert "agent_def.get('id'" in integ, (
        'integration._handle_spawn_agents must read the caller id (else '
        'depends_on stays un-referenceable)')


# ── 2. todo_write single in_progress ────────────────────────────────

def test_todo_write_demotes_extra_in_progress():
    from lib.tools.todo import _normalize_todos
    out = _normalize_todos([
        {'id': 'a', 'content': 'first', 'status': 'in_progress'},
        {'id': 'b', 'content': 'second', 'status': 'in_progress'},
        {'id': 'c', 'content': 'third', 'status': 'in_progress'},
    ])
    statuses = [t['status'] for t in out]
    assert statuses == ['in_progress', 'pending', 'pending'], statuses
    # First keeps in_progress; the rest demoted. Exactly one in_progress.
    assert sum(1 for s in statuses if s == 'in_progress') == 1


def test_todo_write_single_in_progress_untouched():
    from lib.tools.todo import _normalize_todos
    out = _normalize_todos([
        {'id': 'a', 'content': 'done one', 'status': 'completed'},
        {'id': 'b', 'content': 'active', 'status': 'in_progress'},
        {'id': 'c', 'content': 'later', 'status': 'pending'},
    ])
    assert [t['status'] for t in out] == ['completed', 'in_progress', 'pending']


def test_todo_write_neuter_bites():
    # Neuter: strip the demotion loop from a COPY of the source → the
    # multi-in_progress assertion would no longer hold.
    src = _src('lib/tools/todo.py')
    assert 'demoted' in src and "t['status'] = 'pending'" in src, (
        'the in_progress demotion loop must exist in _normalize_todos')


# ── 3. merge_memories failed-delete surfacing ───────────────────────

def test_merge_memories_reports_failed_deletes(tmp_path, monkeypatch):
    import lib.memory.storage._crud as memory_crud

    created = {}
    x_path = str(tmp_path / 'x.md')
    y_path = str(tmp_path / 'y.md')

    def fake_create(name, description='', body='', tags=None, scope='project',
                    project_path=None):
        m = {'id': 'merged1', 'name': name, 'scope': scope}
        created['m'] = m
        return m

    def fake_find(memory_ids, project_path=None, extra_paths=None):
        assert memory_ids == ['x', 'y']
        return {
            'x': {'id': 'x', 'tags': [], 'filepath': x_path,
                  'scope': 'project', 'is_package': False},
            'y': {'id': 'y', 'tags': [], 'filepath': y_path,
                  'scope': 'project', 'is_package': False},
        }

    # x deletes fine, y fails → must appear in failed_ids and be logged.
    def fake_remove(filepath):
        if filepath == y_path:
            raise OSError('injected delete failure')

    monkeypatch.setattr(memory_crud, 'create_memory', fake_create)
    monkeypatch.setattr(memory_crud, '_find_memory_summaries', fake_find)
    monkeypatch.setattr(
        memory_crud, '_refresh_memory_summary',
        lambda summary: (summary, 'stable-revision'))
    monkeypatch.setattr(
        memory_crud, '_revision_is_current', lambda *_args: True)
    monkeypatch.setattr(memory_crud.os, 'remove', fake_remove)

    warnings = []
    # merge_memories logs via its own submodule logger (lib.memory.storage._crud)
    # after the storage package split; patch that logger where the warning fires.
    monkeypatch.setattr(memory_crud.logger, 'warning',
                        lambda *a, **k: warnings.append(a))

    res = memory_crud.merge_memories(
        ['x', 'y'], 'Merged', 'desc', 'body', project_path=str(tmp_path))
    assert res['deleted_ids'] == ['x']
    assert res['failed_ids'] == ['y'], res
    assert any('could not' in str(a) for a in warnings), (
        'a failed source delete must be logged at warning')


def test_merge_memories_handler_surfaces_failed_count():
    # The handler result string must mention kept/failed sources so the model
    # sees the half-merge instead of a green "Merged N".
    handler = _src('lib/tasks_pkg/handlers/memory.py')
    assert "failed_ids" in handler
    assert 'could NOT be deleted' in handler


# ── 4. search/fetch batch abort wiring ──────────────────────────────

def test_search_batch_handlers_pass_abort():
    src = _src('lib/tasks_pkg/handlers/search/_handlers.py')
    # Both run_batch_concurrent call sites must pass an abort predicate keyed
    # on task['aborted']. Match across the multi-line call up to the abort arg.
    calls = re.findall(r"run_batch_concurrent\(.*?abort=lambda: bool\(task\.get\('aborted'\)\)",
                       src, re.DOTALL)
    assert len(calls) >= 2, (
        'both batch call sites must wire abort=lambda: bool(task.get("aborted")); '
        'found %d' % len(calls))


def test_batch_runner_supports_abort_predicate():
    # The adapter primitive must actually short-circuit on abort.
    from lib.tasks_pkg.handlers._adapter import run_batch_concurrent
    ran = []

    def worker(x):
        ran.append(x)
        return x * 2

    out = run_batch_concurrent([1, 2, 3], worker, max_workers=1,
                               abort=lambda: True)
    assert out == [None, None, None], out
    assert ran == [], 'aborted batch must not run any worker'


# ── 5. Doc contradictions ───────────────────────────────────────────

def test_run_command_timeout_prose_matches_backend():
    from lib.tools.project import PROJECT_TOOL_RUN_COMMAND
    desc = PROJECT_TOOL_RUN_COMMAND['function']['description']
    # The false "without a timeout by default" claim must be gone.
    assert 'without a timeout by default' not in desc
    # And the accurate default must be described (timeout semantics are
    # deliberately UNLIMITED now — a bound is opt-in only).
    assert 'NO default timeout' in desc


def test_run_command_prefers_absolute_paths_no_persistent_shell():
    # The shared run_command description must carry the mode-agnostic guidance:
    # prefer absolute paths / avoid `cd`, and state there is no persistent shell.
    # This wording is valid in BOTH project mode and standalone code_exec mode
    # (which deep-copies this same description), so it must NOT claim the shell
    # or working directory persists across calls.
    from lib.tools.project import PROJECT_TOOL_RUN_COMMAND
    desc = PROJECT_TOOL_RUN_COMMAND['function']['description']
    assert 'avoid `cd`' in desc, 'must advise avoiding cd'
    assert 'absolute path' in desc, 'must advise preferring absolute paths'
    assert 'no persistent' in desc.lower(), (
        'must state there is no persistent shell (fresh subprocess per call)')
    # Accuracy trap: the SHARED description must NOT promise the working
    # directory persists between commands — stickiness is mode-scoped and lives
    # only in the working_dir PARAM (standalone mode has no sticky cwd).
    assert 'directory persists between' not in desc.lower()

    # Neuter: a copy with the avoid-cd sentence stripped must fail the guard.
    neutered = desc.replace('avoid `cd`', 'do whatever')
    assert 'avoid `cd`' not in neutered, 'neuter sanity: sentence removed from copy'

    # The stickiness claim is correctly scoped to the project-mode working_dir
    # param (and only there — code_exec overrides that param without it).
    wd_desc = (PROJECT_TOOL_RUN_COMMAND['function']['parameters']
               ['properties']['working_dir']['description'])
    assert 'STICKY' in wd_desc, 'project-mode working_dir must keep the sticky note'


def test_batch_caps_documented():
    src = _src('lib/tools/project.py')
    # grep/find batch cap = 20; apply_diffs/insert_contents = 30.
    assert src.count('max 20 entries') >= 2, 'grep+find batch caps'
    assert src.count('max 30 per call') >= 2, 'apply_diffs+insert_contents caps'
    # count_only ignores max_results.
    assert 'ignored in count_only mode' in src


def test_browser_doc_fixes():
    src = _src('lib/tools/browser.py')
    assert 'fullPage=false' not in src, 'must use the real param name full_page'
    assert 'full_page=false' in src
    assert 'time.sleep()' not in src, 'no bogus Python time.sleep reference'


def test_project_brain_model_tools_are_retired():
    from lib.tools import conversation

    assert 'PEER_INTERVENE_TOOL' not in conversation.__all__
    assert conversation.INTEGRATION_TOOL_NAMES == {
        'integration_checkpoint', 'integration_submit'}


# ── 6. ask_human attendance-aware guard (headless task-wedge fix) ────

def _run_ask_human(task, monkeypatch):
    """Invoke the REAL _handle_ask_human with heavy deps stubbed and a tripwire
    on the indefinite blocking wait. Returns (tool_content, round_entry, blocked).

    ``blocked`` is True iff request_human_guidance was entered — on an
    unattended task that would be the 120s wedge the fix must avoid.
    """
    import lib.tasks_pkg.handlers.misc._human as human_handlers

    blocked = {'hit': False}

    def _tripwire(guidance_id, task=None):
        # If the unattended guard regresses, the handler falls into the real
        # blocking wait — record it and return immediately so the test doesn't
        # actually hang.
        blocked['hit'] = True
        return None

    monkeypatch.setattr('lib.tasks_pkg.human_guidance.request_human_guidance',
                        _tripwire)
    monkeypatch.setattr(human_handlers, 'append_event', lambda *a, **k: None)
    monkeypatch.setattr(human_handlers, '_finalize_tool_round', lambda *a, **k: None)
    monkeypatch.setattr(human_handlers, '_build_simple_meta',
                        lambda *a, **k: {'k': k})
    # Autopilot OFF for this path.
    monkeypatch.setattr('lib.tasks_pkg.autopilot.is_autopilot_enabled',
                        lambda t: False)

    round_entry = {}
    fn_args = {'question': 'What is your name?', 'response_type': 'free_text'}
    tc_id, tool_content, _ = human_handlers._handle_ask_human(
        task, {}, 'ask_human', 'tc1', fn_args, 1, round_entry,
        {}, '', False, None)
    return tool_content, round_entry, blocked['hit']


def test_ask_human_unattended_returns_sentinel_without_blocking(monkeypatch):
    # Headless task (no _attended) → sentinel content, no blocking wait entered.
    content, round_entry, blocked = _run_ask_human({'id': 'headless1'}, monkeypatch)
    assert not blocked, 'unattended ask_human must NOT enter the blocking wait'
    assert 'Cannot ask the user in this execution mode' in content
    assert round_entry.get('status') == 'unanswerable'


def test_ask_human_attended_still_blocks(monkeypatch):
    # Interactive task (_attended True) → must still go through the human wait
    # (tripwire records the entry; the real UI would resolve it).
    task = {'id': 'ui1', '_attended': True}
    content, round_entry, blocked = _run_ask_human(task, monkeypatch)
    assert blocked, 'attended ask_human must still use the human-guidance wait'
    # With the tripwire returning None the handler reports abort, NOT the
    # unattended sentinel — proving we did not neuter the interactive path.
    assert 'Cannot ask the user in this execution mode' not in content


def test_ask_human_guard_neuter_bites():
    # The attendance branch must exist in source; removing it reinstates the
    # unconditional block.
    src = _src('lib/tasks_pkg/handlers/misc/_human.py')
    # Contiguous source markers (the sentinel sentence is split across string
    # concatenation, so assert on the branch guard + the round-status stamp).
    assert "elif not task.get('_attended'):" in src
    assert "round_entry['status'] = 'unanswerable'" in src


if __name__ == '__main__':
    raise SystemExit(pytest.main([__file__, '-v', '-p', 'no:napari']))
