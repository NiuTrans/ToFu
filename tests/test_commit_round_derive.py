"""Per-round modified-file derivation — behaviour tests for commit_round/_derive.

WHY THIS FILE EXISTS
--------------------
``derive_round_modified_files`` decides WHICH FILES a given agent round is
reported to have changed. Everything downstream trusts that answer: the
"files changed" bar in the UI, the round's file-history snapshot attribution,
and the undo/redo surface. Getting it wrong is a *silent* correctness failure —
the round still completes green, it just claims the wrong files.

Measured 2026-07-27 (``coverage run`` over every test that so much as mentions
``commit_round``): ``_derive.py`` at **10%**, ``_commit.py`` at **6%**. The
package was reachable only incidentally — nothing exercised its actual
decisions. That is the gap this file closes.

WHY THIS MODULE FIRST (and not the ones the epic ranked above it)
    The epic ranked ``_core_schema/_tables.py`` as priority #1 on the strength
    of "565 LOC, zero test references". Real coverage measurement says
    ``_tables.py`` is at **100%** — ``tests/test_core_schema_{groundwork,parity}``
    drive it thoroughly; the static census had merely failed to see it because
    no test names that *file*. So the epic's #1 was a false alarm, and this —
    the round-commit path it ranked #2 — is the real top gap. Recorded here
    because the next person to read the epic deserves the correction.

WHAT IS ASSERTED (behaviour, never implementation — charter discipline)
    Each test states an OUTCOME the caller depends on, so a legitimate rewrite
    of the internals keeps them green while a semantic regression turns them
    red. No source-text anchors, no private-symbol assertions.

  1. Conversation isolation — a mod stamped with ANOTHER task's id is never
     attributed to this round. This is the leak that once let a foreign file
     appear in a round's file list.
  2. Extra-root aggregation — the journal is keyed per-root, so a write to an
     extra workspace root lives in a DIFFERENT journal. Scanning only the
     primary root makes those edits invisible (the documented bug this
     function was written to fix).
  3. Timestamp fallback is a LAST resort — used only when the task owns no
     stamped mod at all, and it must set the ``used_ts_fallback`` flag so the
     caller knows the answer is heuristic.
  4. Action classification — write/patch/insert/delete map to the vocabulary
     the frontend renders; a ``run_command`` delete is distinguished from a
     modify by resolving against the mod's OWN root, not the primary.
  5. Deduplication is keyed on ``(root, path)`` — the same relative path in two
     different roots is two files, not one.

Run::

    PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest \
        tests/test_commit_round_derive.py -p no:cacheprovider -q
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lib.tasks_pkg.commit_round._derive import derive_round_modified_files  # noqa: E402

pytestmark = pytest.mark.unit

TASK_ID = 'task-under-test'
OTHER_TASK = 'task-of-a-concurrent-conversation'


def _task(**over):
    t = {'id': TASK_ID, 'convId': 'conv-1', 'created_at': 1000}
    t.update(over)
    return t


def _mod(path, mtype='write_file', task_id=TASK_ID, **over):
    m = {'path': path, 'type': mtype, 'taskId': task_id,
         'timestamp': 2000, 'existed': True}
    m.update(over)
    return m


@pytest.fixture
def journal(monkeypatch):
    """Install a fake per-root modifications journal.

    ``derive_round_modified_files`` imports ``get_modifications`` from
    ``lib.project_mod`` INSIDE the function body, so patching the attribute on
    that package is what the call actually resolves. Returns a dict the test
    fills as ``{root_path: [mod, ...]}``.
    """
    store: dict[str, list] = {}

    def fake_get_modifications(root, conv_id=None):
        return list(store.get(root, []))

    import lib.project_mod as pm
    monkeypatch.setattr(pm, 'get_modifications', fake_get_modifications)
    return store


def _paths(file_list):
    return sorted(f['path'] for f in file_list)


def _action_of(file_list, path):
    for f in file_list:
        if f['path'] == path:
            return f['action']
    raise AssertionError(f'{path!r} not in {file_list!r}')


# ── 1. conversation isolation ─────────────────────────────────────────

def test_other_tasks_mods_are_never_attributed_to_this_round(journal):
    """A mod stamped with another task's id must not appear in our file list.

    Two conversations pointing at the same project root share one journal, so
    without the taskId filter a concurrent session's edit shows up as ours.
    """
    journal['/proj'] = [
        _mod('mine.py'),
        _mod('theirs.py', task_id=OTHER_TASK),
    ]
    files, count, used_ts = derive_round_modified_files(_task(), '/proj', ['/proj'])
    assert _paths(files) == ['mine.py'], 'a concurrent task\'s edit leaked in'
    assert count == 1
    assert used_ts is False, 'stamped mods exist — the ts fallback must stay off'


def test_no_mods_at_all_yields_empty_answer(journal):
    journal['/proj'] = []
    files, count, used_ts = derive_round_modified_files(_task(), '/proj', ['/proj'])
    assert files == [] and count == 0 and used_ts is False


# ── 2. extra-root aggregation ─────────────────────────────────────────

def test_edits_in_extra_roots_are_included(journal):
    """The journal is per-root; an extra-root write lives in ITS OWN journal.

    Scanning only the primary root is precisely the bug this function exists to
    fix: extra-root edits went missing, and the file-history side channel then
    back-filled the gap with a concurrent conversation's edit.
    """
    journal['/primary'] = [_mod('a.py')]
    journal['/extra'] = [_mod('b.py', root='extra')]
    files, count, _ = derive_round_modified_files(
        _task(), '/primary', ['/primary', '/extra'])
    assert _paths(files) == ['a.py', 'b.py'], 'extra-root edit is invisible'
    assert count == 2


def test_duplicate_root_entries_are_scanned_once(journal):
    """A root repeated in projectPaths must not double-count its mods."""
    journal['/primary'] = [_mod('a.py')]
    files, count, _ = derive_round_modified_files(
        _task(), '/primary', ['/primary', '/primary'])
    assert _paths(files) == ['a.py']
    assert count == 1, 'the same root was scanned twice'


def test_primary_root_none_still_scans_extra_roots(journal):
    """A project with no primary root (extra-roots-only config) is still scanned."""
    journal['/extra'] = [_mod('only.py', root='extra')]
    files, count, _ = derive_round_modified_files(_task(), None, [None, '/extra'])
    assert _paths(files) == ['only.py']
    assert count == 1


# ── 3. timestamp fallback is a last resort ────────────────────────────

def test_ts_fallback_used_only_when_no_stamped_mod_exists(journal):
    """Legacy mods carry no taskId, so a start-time filter is the only option —
    and the caller MUST be told the answer is heuristic."""
    journal['/proj'] = [
        {'path': 'legacy.py', 'type': 'write_file', 'timestamp': 5000},
        {'path': 'ancient.py', 'type': 'write_file', 'timestamp': 500},
    ]
    files, count, used_ts = derive_round_modified_files(
        _task(created_at=1000), '/proj', ['/proj'])
    assert _paths(files) == ['legacy.py'], 'pre-task-start mod must be excluded'
    assert used_ts is True, 'heuristic answer not flagged to the caller'
    assert count == 1


def test_stamped_mod_suppresses_ts_fallback_in_same_root(journal):
    """When the task owns a stamped mod, unstamped ones are NOT swept in.

    Otherwise a long-lived project's legacy journal entries would attach
    themselves to whichever round happened to run after them.
    """
    journal['/proj'] = [
        _mod('stamped.py'),
        {'path': 'unstamped.py', 'type': 'write_file', 'timestamp': 9000},
    ]
    files, _, used_ts = derive_round_modified_files(
        _task(created_at=1000), '/proj', ['/proj'])
    assert _paths(files) == ['stamped.py']
    assert used_ts is False


# ── 4. action classification ──────────────────────────────────────────

@pytest.mark.parametrize('mtype,existed,expected', [
    ('write_file', False, 'created'),
    ('write_file', True, 'written'),
    ('apply_diff', True, 'patched'),
    ('apply_diffs', True, 'patched'),
    ('insert_content', True, 'inserted'),
    ('insert_contents', True, 'inserted'),
])
def test_tool_type_maps_to_rendered_action(journal, mtype, existed, expected):
    """The action strings are a contract with the frontend's file bar."""
    journal['/proj'] = [_mod('f.py', mtype=mtype, existed=existed)]
    files, _, _ = derive_round_modified_files(_task(), '/proj', ['/proj'])
    assert _action_of(files, 'f.py') == expected


def test_run_command_delete_is_detected_against_the_mods_own_root(tmp_path, journal):
    """A ``run_command`` that removed a file classifies as 'deleted'.

    The existence probe MUST resolve against the mod's own ``basePath``. Using
    the primary root instead would misclassify an extra-root delete.

    ★ ANCHOR NOTE (this test caught its own blind spot). The first version used
    a non-existent ``/primary`` as the primary root — but then
    ``exists('/primary/gone.py')`` is ALSO False, so probing the wrong root
    produced the same 'deleted' verdict and the NEUTER did not bite: swapping
    ``basePath`` for ``project_path`` in the shipped code left all 19 tests
    green. The two code paths have to be made to DISAGREE, so the primary root
    here is a real directory that still CONTAINS ``gone.py``. Now probing the
    primary says "exists → modified" while probing the mod's own root says
    "gone → deleted", and only the correct implementation passes.
    """
    primary = tmp_path / 'primary'
    primary.mkdir()
    # Same relative path still present under the PRIMARY root — this is what
    # makes a wrong-root probe observably wrong.
    (primary / 'gone.py').write_text('still here', encoding='utf-8')
    extra = tmp_path / 'extra'
    extra.mkdir()
    journal[str(primary)] = []
    journal[str(extra)] = [
        _mod('gone.py', mtype='run_command', existed=True,
             originalContent='old', basePath=str(extra)),
    ]
    files, _, _ = derive_round_modified_files(
        _task(), str(primary), [str(primary), str(extra)])
    assert _action_of(files, 'gone.py') == 'deleted', (
        'the delete was classified against the wrong root — a file that only '
        'survives under the PRIMARY root made an extra-root delete look like '
        'a modify')


def test_run_command_surviving_file_is_modified_not_deleted(tmp_path, journal):
    live = tmp_path / 'root'
    live.mkdir()
    (live / 'kept.py').write_text('x', encoding='utf-8')
    journal[str(live)] = [
        _mod('kept.py', mtype='run_command', existed=True,
             originalContent='old', basePath=str(live)),
    ]
    files, _, _ = derive_round_modified_files(
        _task(), str(live), [str(live)])
    assert _action_of(files, 'kept.py') == 'modified'


def test_unknown_tool_type_passes_through_as_its_own_action(journal):
    """An unrecognised mod type must not be dropped — the file DID change."""
    journal['/proj'] = [_mod('f.py', mtype='some_future_tool')]
    files, _, _ = derive_round_modified_files(_task(), '/proj', ['/proj'])
    assert _action_of(files, 'f.py') == 'some_future_tool'


# ── 5. dedup keyed on (root, path) ────────────────────────────────────

def test_same_path_in_two_roots_stays_two_files(journal):
    """``src/main.py`` in two workspace roots is two distinct files."""
    journal['/a'] = [_mod('src/main.py', root='alpha')]
    journal['/b'] = [_mod('src/main.py', root='beta')]
    files, count, _ = derive_round_modified_files(_task(), '/a', ['/a', '/b'])
    assert len(files) == 2, 'same path in different roots was collapsed'
    assert {f['root'] for f in files} == {'alpha', 'beta'}
    assert count == 2, 'the raw mod count must reflect both writes'


def test_repeated_writes_to_one_file_collapse_to_the_last_action(journal):
    """Multiple writes to one path yield ONE entry — the final state wins.

    ``count`` is the UNIQUE-FILE count, not the raw journal-event count: it
    headlines the "N files changed" card directly above this deduped list, so
    a file edited twice must read as one changed file, not two (2026-08-27:
    the card showed "2 files changed" over a one-row list).
    """
    journal['/proj'] = [
        _mod('f.py', mtype='write_file', existed=False),   # created
        _mod('f.py', mtype='apply_diff'),                  # then patched
    ]
    files, count, _ = derive_round_modified_files(_task(), '/proj', ['/proj'])
    assert len(files) == 1
    assert _action_of(files, 'f.py') == 'patched', 'last write must win'
    assert count == 1, 'count must match the deduped list the card renders'


def test_unrooted_mod_omits_the_root_key_entirely(journal):
    """A mod with no root name yields an entry WITHOUT a ``root`` key.

    Downstream dedup in ``_commit.py`` treats a missing root as the unrooted
    alias; emitting ``root: ''`` instead would create a second, non-matching
    key and duplicate the row in the UI's file bar.
    """
    journal['/proj'] = [_mod('f.py', root='')]
    files, _, _ = derive_round_modified_files(_task(), '/proj', ['/proj'])
    assert files == [{'path': 'f.py', 'action': 'written'}]


if __name__ == '__main__':
    sys.exit(pytest.main([__file__, '-v']))
