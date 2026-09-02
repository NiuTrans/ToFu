"""Concurrency regression: undo / undo-all / redo must resolve the target
project from the ROUND being acted on — never from the mutable globally-active
UI project (``_state['path']``).

Scenario (the exact multi-conversation trap):
  * Conversation A edits project A (task-A).
  * Conversation B edits project B (task-B).
  * The UI is focused on project B, so ``_state['path']`` points at B.
  * The user clicks Undo / Undo-All / Redo on conversation A's round.

Before the fix, ``/redo`` (and ``/undo-all`` with no explicit path) resolved
the project via ``_state['path']`` → project B → acted on the wrong project
(redo found no snapshot; undo-all wiped the wrong project). After the fix,
``resolve_base_path(task_id)`` recovers project A from the round's own record,
so the action targets A and B is left untouched.

This drives the REAL modification-recording + undo/redo functions and the
REAL ``resolve_base_path`` resolver. The NC blocks assert that WITHOUT the
resolver (i.e. falling back to ``_state['path']``) the action lands on the
wrong project — proving the resolver is load-bearing.
"""
from __future__ import annotations

import os
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from lib.project_mod import config as pm_config  # noqa: E402
from lib.project_mod import modifications as pm_modifications  # noqa: E402
from lib.project_mod.modifications import (  # noqa: E402
    redo_task_modifications,
    resolve_base_path,
    undo_all_modifications,
    undo_task_modifications,
)
from lib.project_mod.write_tools import tool_write_file  # noqa: E402


def _register_primary(name, abs_path):
    with pm_config._lock:
        pm_config._roots.clear()
        pm_config._roots[name] = pm_config._make_root_state(abs_path)
        pm_config._state['path'] = os.path.abspath(abs_path)


def _register_extra(name, abs_path):
    with pm_config._lock:
        pm_config._roots[name] = pm_config._make_root_state(abs_path)


def _focus_ui_on(abs_path):
    """Simulate the UI globally focusing on a project."""
    with pm_config._lock:
        pm_config._state['path'] = os.path.abspath(abs_path)


def _cleanup():
    with pm_config._lock:
        pm_config._roots.clear()
        pm_config._state['path'] = ''


def _resolve_like_route(task_id, explicit_path=''):
    """Reproduce the route's concurrency-safe resolution EXACTLY.

    Mirrors ``project_undo`` / ``project_redo``: explicit path wins, else
    recover from the round's record, NEVER ``_state['path']``.
    """
    if explicit_path:
        return explicit_path
    return resolve_base_path(task_id=task_id or None) or ''


_UNIQ = [0]


def _seed_two_projects():
    """Create project A + B, edit each from its own conversation/task.

    Returns (dir_a, dir_b, task_a, task_b).

    Task/conv ids are GLOBALLY unique (pid + monotonic ns + counter) so
    ``resolve_base_path`` (which scans the shared, persistent ``SESSIONS_DIR``)
    can never match a LEFTOVER record — neither from an earlier test in this
    run NOR from a previous pytest run that reused a plain ``task-a-1`` id but
    a now-deleted tmp dir. Real taskIds are globally unique too, so this
    mirrors production.
    """
    _UNIQ[0] += 1
    tag = f'{os.getpid()}-{time.monotonic_ns()}-{_UNIQ[0]}'
    task_a, task_b = f'task-a-{tag}', f'task-b-{tag}'
    conv_a, conv_b = f'conv-a-{tag}', f'conv-b-{tag}'
    dir_a = tempfile.mkdtemp(prefix='proj_a_')
    dir_b = tempfile.mkdtemp(prefix='proj_b_')
    # Register A as primary, B as an extra root (both known to the registry).
    _register_primary('proj-a', dir_a)
    _register_extra('proj-b', dir_b)

    r1 = tool_write_file(dir_a, 'a.txt', 'A-new\n',
                         conv_id=conv_a, task_id=task_a)
    assert r1['ok'], r1
    r2 = tool_write_file(dir_b, 'b.txt', 'B-new\n',
                         conv_id=conv_b, task_id=task_b)
    assert r2['ok'], r2
    return dir_a, dir_b, task_a, task_b


# ── resolve_base_path recovers the right project regardless of UI focus ──

def test_resolve_base_path_ignores_ui_focus():
    dir_a, dir_b, task_a, task_b = _seed_two_projects()
    try:
        _focus_ui_on(dir_b)  # UI is on B
        # Resolving A's round must return A, not the UI-focused B.
        resolved = _resolve_like_route(task_a)
        assert os.path.abspath(resolved) == os.path.abspath(dir_a), resolved
        # And B's round resolves to B.
        resolved_b = _resolve_like_route(task_b)
        assert os.path.abspath(resolved_b) == os.path.abspath(dir_b), resolved_b
    finally:
        _cleanup()


def test_NC_ui_focus_would_target_wrong_project():
    """NEGATIVE CONTROL: the OLD behaviour (fall back to _state['path'])
    resolves A's undo onto project B — the exact silent-no-op bug."""
    dir_a, dir_b, task_a, task_b = _seed_two_projects()
    try:
        _focus_ui_on(dir_b)
        # Simulate the buggy resolution: explicit path empty AND we use the
        # global UI focus instead of resolve_base_path.
        buggy_target = pm_config._state['path']
        assert os.path.abspath(buggy_target) == os.path.abspath(dir_b)
        # Undoing task-a against the WRONG project (B) is a silent no-op.
        res = undo_task_modifications(buggy_target, task_a)
        assert res['ok'] and res['undone'] == 0, res  # nothing reverted → the bug
    finally:
        _cleanup()


# ── undo of A's round targets A, leaves B untouched ──

def test_undo_task_targets_correct_project_under_concurrency():
    dir_a, dir_b, task_a, task_b = _seed_two_projects()
    try:
        _focus_ui_on(dir_b)
        target = _resolve_like_route(task_a)
        res = undo_task_modifications(target, task_a)
        assert res['ok'] and res['undone'] == 1, res
        # A restored (file created by the round → deleted on undo).
        assert not os.path.exists(os.path.join(dir_a, 'a.txt'))
        # B untouched.
        assert os.path.exists(os.path.join(dir_b, 'b.txt'))
        with open(os.path.join(dir_b, 'b.txt')) as f:
            assert f.read() == 'B-new\n'
    finally:
        _cleanup()


def test_stale_reverse_patch_fails_without_rewriting_the_file():
    with tempfile.TemporaryDirectory() as project:
        path = os.path.join(project, 'document.txt')
        with open(path, 'w', encoding='utf-8') as handle:
            handle.write('newer external edit\n')
        undone, failed = pm_modifications._undo_modifications_list(project, [{
            'type': 'apply_diff',
            'path': 'document.txt',
            'reversePatch': {'search': 'tool replacement',
                             'replace': 'original text'},
        }])

        assert undone == []
        assert failed == [{
            'path': 'document.txt',
            'reason': 'Reverse patch target no longer matches',
        }]
        with open(path, encoding='utf-8') as handle:
            assert handle.read() == 'newer external edit\n'


# ── undo-all with a pinned project only touches THAT project ──

def test_undo_all_scoped_to_pinned_project():
    dir_a, dir_b, task_a, task_b = _seed_two_projects()
    try:
        _focus_ui_on(dir_b)  # UI on B
        # Frontend pins conv A's project → undo-all must scope to A only.
        target = _resolve_like_route('', explicit_path=dir_a)
        res = undo_all_modifications(target)
        assert res['ok'] and res['undone'] == 1, res
        assert not os.path.exists(os.path.join(dir_a, 'a.txt'))  # A wiped
        assert os.path.exists(os.path.join(dir_b, 'b.txt'))      # B intact
    finally:
        _cleanup()


# ── redo resolves the right project (explicit pin wins over UI focus) ──
#
# NOTE on real semantics: undo DELETES the round's modification record, so
# after an undo ``resolve_base_path(task_id)`` can no longer recover the
# project from that record. The REAL frontend therefore pins the
# conversation's own ``projectPath`` on the redo request (explicit_path). The
# route's resolution order (explicit → resolver → NEVER _state['path']) means
# an explicit pin is the authoritative redo path. These tests assert the redo
# route resolves to project A via that pin even while the UI is focused on B —
# they do NOT re-test file_history's blob restore (covered by fh's own suite).

def test_redo_resolves_project_via_pin_not_ui_focus():
    dir_a, dir_b, task_a, task_b = _seed_two_projects()
    try:
        undo_task_modifications(_resolve_like_route(task_a), task_a)
        assert not os.path.exists(os.path.join(dir_a, 'a.txt'))

        _focus_ui_on(dir_b)  # UI now on B — the trap for redo
        # Frontend pins conv A's project → the route resolves to A regardless
        # of the UI focus (this is exactly what project_redo now does).
        redo_target = _resolve_like_route(task_a, explicit_path=dir_a)
        assert os.path.abspath(redo_target) == os.path.abspath(dir_a)
        res = redo_task_modifications(redo_target, task_a)
        # The invariant is that redo was DISPATCHED to project A, not B. Whether
        # the fh snapshot exists depends on whether a round-commit ran; either
        # way it must NOT have touched project B.
        assert res.get('taskId') == task_a, res
        assert not os.path.exists(os.path.join(dir_b, 'a.txt'))  # never wrote into B
        assert os.path.exists(os.path.join(dir_b, 'b.txt'))      # B intact
    finally:
        _cleanup()


def test_redo_resolver_survives_pending_modification_removal():
    """The v3 turn command can resolve redo after an Undo + page reload."""
    from lib import file_history

    dir_a, dir_b, task_a, task_b = _seed_two_projects()
    try:
        snapshot_id = file_history.make_snapshot(
            dir_a,
            task_id=task_a,
            conv_id=f"conv-for-{task_a}",
            rel_paths=["a.txt"],
        )
        assert snapshot_id
        result = undo_task_modifications(resolve_base_path(task_id=task_a), task_a)
        assert result["ok"] and result["undone"] == 1

        _focus_ui_on(dir_b)
        resolved = resolve_base_path(task_id=task_a)
        assert os.path.abspath(resolved) == os.path.abspath(dir_a)
    finally:
        _cleanup()


def test_NC_redo_against_ui_focus_wrong_project():
    """NEGATIVE CONTROL: redoing task-a against the UI-focused project (B) —
    the OLD ``_active_project_path('')`` → ``_state['path']`` behaviour — can
    never find task-a's snapshot, proving resolving from the round (explicit
    pin / resolver), not UI focus, is what makes redo land on the right
    project."""
    dir_a, dir_b, task_a, task_b = _seed_two_projects()
    try:
        undo_task_modifications(_resolve_like_route(task_a), task_a)
        _focus_ui_on(dir_b)
        # Buggy path: redo against _state['path'] (B).
        res = redo_task_modifications(pm_config._state['path'], task_a)
        # Never succeeds on the wrong project; A's file stays absent.
        assert not res.get('ok'), res
        assert not os.path.exists(os.path.join(dir_a, 'a.txt'))
    finally:
        _cleanup()


if __name__ == '__main__':
    test_resolve_base_path_ignores_ui_focus()
    test_NC_ui_focus_would_target_wrong_project()
    test_undo_task_targets_correct_project_under_concurrency()
    test_undo_all_scoped_to_pinned_project()
    test_redo_resolves_project_via_pin_not_ui_focus()
    test_NC_redo_against_ui_focus_wrong_project()
    print('All tests passed.')
