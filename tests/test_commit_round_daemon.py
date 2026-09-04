"""Round-commit daemon: attribution filter, spawn gates, message patching.

WHY THIS FILE EXISTS
────────────────────
`commit_round/_commit.py` + `_profile.py` are the per-round LANDING point: they
snapshot the file-history, decide which side-channel file edits belong to THIS
round, emit `round_committed`, and fold post-settlement facts into the settled
turn projection after `persist_task_result` has already run. A regression loses
task results that nothing else can reconstruct.

Measured before this file existed: `_commit.py` **6%**, `_profile.py` **13%** —
the package was reachable only incidentally.

DESIGN: why no real threads
    Both modules are split into `_spawn_*` (starts a daemon thread) and
    `_run_*_async` (the thread BODY). Tests replace the concrete consumer
    bindings in `_commit` / `_profile`, then drive the BODY synchronously and assert the
    decisions, and cover the spawn functions only for their GATE conditions.
    That keeps every assertion deterministic — a thread-timing test would be
    flaky and would not check any more logic.

THE ATTRIBUTION FILTER (Fix 2 in the source) IS THE HIGH-VALUE TARGET
    The file-history diff is computed against the PRIMARY root's project-global
    snapshot index, which a CONCURRENT conversation on the same project also
    writes to. So a raw diff contains other tasks' edits. The filter keeps a
    path only when it is provably ours:

      * `last_writer_task_id == this task`  → ours, keep;
      * writer EMPTY **and** this round ran an opaque writer (code_exec / MCP /
        unknown tool that can edit without stamping) → plausibly ours, keep
        (fail-open: never suppress a genuine side-channel edit);
      * writer EMPTY and the round ran ONLY read-only / self-stamping tools →
        cannot be ours, DROP. This is the cross-conversation leak that once let
        a foreign file appear in a round while its own extra-root edits were
        missing.

    Every one of those three outcomes is silent when wrong, which is exactly
    why they are pinned here.
"""

import sys

import pytest

from lib.tasks_pkg.commit_round import _commit as commit_mod
from lib.tasks_pkg.commit_round import _profile as profile_mod

pytestmark = pytest.mark.unit

TASK_ID = 'task-abcdef123456'
OTHER_TASK = 'task-of-a-concurrent-conversation'


def _task(**over):
    t = {'id': TASK_ID, 'convId': 'conv-1', '_userId': 1, 'toolRounds': []}
    t.update(over)
    return t


def _round(tool_name):
    return {'toolName': tool_name}


# ══════════════════════════════════════════════════════════════════
#  Spawn gates — a daemon must NOT be started when preconditions fail
# ══════════════════════════════════════════════════════════════════

@pytest.fixture
def spawned(monkeypatch):
    """Record Thread(...) construction instead of starting anything."""
    calls = []

    class _FakeThread:
        def __init__(self, **kw):
            calls.append(kw)

        def start(self):
            calls[-1]['started'] = True

    monkeypatch.setattr(commit_mod.threading, 'Thread', _FakeThread)
    return calls


def test_commit_spawn_requires_project_enabled_path_and_task_id(spawned):
    """All three preconditions gate the daemon; none may be assumed."""
    commit_mod._spawn_async_commit_round(_task(), False, '/proj')          # disabled
    commit_mod._spawn_async_commit_round(_task(), True, None)              # no path
    commit_mod._spawn_async_commit_round(_task(id=''), True, '/proj')      # no task id
    assert spawned == []

    commit_mod._spawn_async_commit_round(_task(), True, '/proj')
    assert len(spawned) == 1 and spawned[0].get('started') is True


def test_commit_spawn_thread_is_a_daemon(spawned):
    """A non-daemon thread would keep the process alive on shutdown."""
    commit_mod._spawn_async_commit_round(_task(), True, '/proj')
    assert spawned[0]['daemon'] is True


def test_commit_spawn_captures_opaque_writer_before_terminal_release(spawned):
    """The daemon must not retain or later reread the structural projection."""
    task = _task(toolRounds=[_round('code_exec')])
    commit_mod._spawn_async_commit_round(task, True, '/proj')

    daemon_args = spawned[0]['args']
    assert daemon_args[3] is True
    task['toolRounds'] = None
    assert daemon_args[3] is True


def test_commit_spawn_failure_is_swallowed(monkeypatch):
    """Failing to spawn must not break the round — the snapshot is best-effort.

    This runs on the loop-exit → `done` path; raising here would turn a
    completed round into a failed one over a missing snapshot.
    """
    def boom(**kw):
        raise RuntimeError('cannot start thread')

    monkeypatch.setattr(commit_mod.threading, 'Thread', boom)
    commit_mod._spawn_async_commit_round(_task(), True, '/proj')   # must not raise


# ══════════════════════════════════════════════════════════════════
#  Attribution filter (Fix 2) — the cross-conversation leak guard
# ══════════════════════════════════════════════════════════════════

@pytest.fixture
def fh_env(monkeypatch):
    """Stub the file-history + journal layer the daemon body drives.

    Returns a mutable dict the test fills:
      diff    — what diff_name_status reports (the raw, possibly-foreign set)
      tracked — path → {'last_writer_task_id': ...} attribution index
      events  — captured append_event frames
    """
    import contextlib
    import types

    env = {'diff': [], 'tracked': {}, 'events': [], 'saved': None,
           'snap_id': 'snap-1111'}

    fake_fh = types.SimpleNamespace(
        is_enabled=lambda: True,
        get_last_snapshot_id=lambda p: 'snap-0000',
        make_snapshot=lambda p, **kw: env['snap_id'],
        diff_name_status=lambda p, a, b: list(env['diff']),
    )
    fake_store = types.SimpleNamespace(
        _project_lock=lambda p: contextlib.nullcontext(),
        load_tracked=lambda p: dict(env['tracked']),
    )

    real_import = __import__

    def fake_import(name, *a, **kw):
        if name == 'lib.file_history':
            return types.SimpleNamespace(file_history=fake_fh)
        return real_import(name, *a, **kw)

    monkeypatch.setitem(sys.modules, 'lib.file_history', fake_fh)
    # The daemon body resolves `from lib import file_history as fh`, which
    # binds via getattr(lib, 'file_history') FIRST — if ANY earlier import in
    # this worker already set the package attribute to the REAL module (test
    # order / xdist distribution dependent — this exact divergence red-filed
    # public CI while every local run was green), the sys.modules stub is
    # ignored and the real make_snapshot fires on '/proj'. Pin the attribute
    # too so both resolution paths yield the fake.
    import lib as _lib_pkg
    monkeypatch.setattr(_lib_pkg, 'file_history', fake_fh, raising=False)
    monkeypatch.setitem(sys.modules, 'lib.file_history.store', fake_store)
    monkeypatch.setitem(sys.modules, 'lib.project_mod',
                        types.SimpleNamespace(
                            get_modifications=lambda root, conv_id=None: []))
    monkeypatch.setattr(commit_mod, 'append_event',
                        lambda task, evt: env['events'].append(evt))
    return env


def _run(task, env, project='/proj'):
    commit_mod._run_commit_round_async(task, project)
    return env['events']


def test_other_tasks_side_channel_edit_is_dropped(fh_env):
    """A path attributed to a CONCURRENT task must never enter our file list.

    Two conversations on the same project root share the snapshot index, so the
    raw diff legitimately contains their edits.
    """
    fh_env['diff'] = [{'path': 'theirs.py', 'action': 'modified'}]
    fh_env['tracked'] = {'theirs.py': {'last_writer_task_id': OTHER_TASK}}
    task = _task(toolRounds=[_round('code_exec')])   # opaque writer present
    _run(task, fh_env)
    assert 'modifiedFileList' not in task or task['modifiedFileList'] == []


def test_our_own_attributed_edit_is_kept(fh_env):
    fh_env['diff'] = [{'path': 'mine.py', 'action': 'modified'}]
    fh_env['tracked'] = {'mine.py': {'last_writer_task_id': TASK_ID}}
    task = _task()
    _run(task, fh_env)
    assert [f['path'] for f in task['modifiedFileList']] == ['mine.py']


def test_unattributed_edit_kept_when_round_ran_an_opaque_writer(fh_env):
    """code_exec / MCP can edit files WITHOUT stamping attribution, so an
    unattributed path on such a round is plausibly ours — fail OPEN so a real
    side-channel edit is never suppressed."""
    fh_env['diff'] = [{'path': 'made_by_code_exec.txt', 'action': 'created'}]
    fh_env['tracked'] = {'made_by_code_exec.txt': {'last_writer_task_id': ''}}
    task = _task(toolRounds=[_round('code_exec')])
    _run(task, fh_env)
    assert [f['path'] for f in task['modifiedFileList']] == ['made_by_code_exec.txt']


def test_captured_opaque_writer_survives_terminal_round_release(fh_env):
    """Production passes the pre-release fact, not the released projection."""
    fh_env['diff'] = [{'path': 'made_before_release.txt', 'action': 'created'}]
    fh_env['tracked'] = {
        'made_before_release.txt': {'last_writer_task_id': ''},
    }
    task = _task(toolRounds=None)

    commit_mod._run_commit_round_async(
        task,
        '/proj',
        round_has_opaque_writer=True,
    )

    assert [f['path'] for f in task['modifiedFileList']] == [
        'made_before_release.txt',
    ]


def test_unattributed_edit_dropped_when_round_ran_only_readonly_tools(fh_env):
    """THE cross-conversation leak fix: a round that only READ cannot own an
    unstamped edit, so that path is another session's drift and must be dropped.

    Without this, a foreign file appeared in the round's "files changed" bar.
    """
    fh_env['diff'] = [{'path': 'someone_elses.py', 'action': 'modified'}]
    fh_env['tracked'] = {'someone_elses.py': {'last_writer_task_id': ''}}
    task = _task(toolRounds=[_round('read_files'), _round('grep_search')])
    _run(task, fh_env)
    assert 'modifiedFileList' not in task or task['modifiedFileList'] == []


def test_commit_round_emits_linear_git_settlement_receipt(
        fh_env, monkeypatch):
    import lib.linear_git_checkpoint as linear_checkpoint

    receipt = {
        'schema': 'tofu.linear-git-checkpoint/v2',
        'status': 'committed',
        'repositories': [{
            'status': 'committed', 'checkpointSha': 'abc123',
            'stableUpdated': True,
        }],
    }
    calls = []

    def _settle(task, *, user_id, project_path, project_paths):
        calls.append((task['id'], user_id, project_path, project_paths))
        return receipt

    monkeypatch.setattr(
        linear_checkpoint, 'settle_task_checkpoint', _settle)
    task = _task()
    events = _run(task, fh_env)

    assert calls == [(TASK_ID, 1, '/proj', None)]
    committed = [event for event in events
                 if event.get('type') == 'round_committed']
    assert committed
    assert committed[-1]['linearGitCheckpoint'] == receipt


def test_checkpoint_exception_does_not_rewrite_settled_task(
        fh_env, monkeypatch):
    import lib.linear_git_checkpoint as linear_checkpoint

    def _explode(*_args, **_kwargs):
        raise RuntimeError('Git checkpoint unavailable')

    monkeypatch.setattr(
        linear_checkpoint, 'settle_task_checkpoint', _explode)
    task = _task(status='done', error='')

    _run(task, fh_env)

    assert task['status'] == 'done'
    assert task['error'] == ''
    assert '_linearGitCheckpoint' not in task


def test_self_stamping_edit_tools_do_not_count_as_opaque(fh_env):
    """write_file / apply_diff stamp their own attribution, so a round using
    only them leaves no unattributed edit of its own — an unattributed path is
    therefore still foreign."""
    fh_env['diff'] = [{'path': 'foreign.py', 'action': 'modified'}]
    fh_env['tracked'] = {'foreign.py': {'last_writer_task_id': ''}}
    task = _task(toolRounds=[_round('write_file'), _round('apply_diff')])
    _run(task, fh_env)
    assert 'modifiedFileList' not in task or task['modifiedFileList'] == []


def test_unknown_tool_name_is_treated_as_opaque_fail_open(fh_env):
    """A custom MCP tool is an unknown name; it MAY write without stamping, so
    the probe must fail open rather than suppress a genuine edit."""
    fh_env['diff'] = [{'path': 'from_mcp.txt', 'action': 'created'}]
    fh_env['tracked'] = {'from_mcp.txt': {'last_writer_task_id': ''}}
    task = _task(toolRounds=[_round('mcp__something__do_a_thing')])
    _run(task, fh_env)
    assert [f['path'] for f in task['modifiedFileList']] == ['from_mcp.txt']


def test_malformed_tool_rounds_do_not_break_the_probe(fh_env):
    """toolRounds carries rows from several producers; a non-dict entry must be
    skipped, not crash the daemon (which would lose the whole snapshot)."""
    fh_env['diff'] = [{'path': 'x.txt', 'action': 'created'}]
    fh_env['tracked'] = {'x.txt': {'last_writer_task_id': TASK_ID}}
    task = _task(toolRounds=['garbage', None, {'no_tool_name': 1},
                             _round('code_exec')])
    _run(task, fh_env)
    assert [f['path'] for f in task['modifiedFileList']] == ['x.txt']


# ══════════════════════════════════════════════════════════════════
#  round_committed event + snapshot id propagation
# ══════════════════════════════════════════════════════════════════

def test_snapshot_id_is_stamped_on_task_under_both_names(fh_env):
    """`snapshotId` is canonical; `gitSha` is kept for frontend back-compat.
    Dropping either silently breaks the undo/redo surface."""
    task = _task()
    _run(task, fh_env)
    assert task['snapshotId'] == 'snap-1111'
    assert task['gitSha'] == 'snap-1111'


def test_round_committed_event_is_emitted_with_ids(fh_env):
    events = _run(_task(), fh_env)
    assert len(events) == 1
    evt = events[0]
    assert evt.get('snapshotId') == 'snap-1111'
    assert evt.get('gitSha') == 'snap-1111'
    assert evt.get('taskId') == TASK_ID


def test_no_snapshot_means_no_event_and_no_stamp(fh_env):
    """A no-op / disabled snapshot must not emit a phantom round_committed."""
    fh_env['snap_id'] = ''
    task = _task()
    events = _run(task, fh_env)
    assert events == []
    assert 'snapshotId' not in task


def test_added_paths_are_reported_on_the_event_for_live_clients(fh_env):
    """The SSE reader may still be attached; the amend event carries the
    enriched list so a live client sees the side-channel files too."""
    fh_env['diff'] = [{'path': 'extra.txt', 'action': 'created'}]
    fh_env['tracked'] = {'extra.txt': {'last_writer_task_id': TASK_ID}}
    evt = _run(_task(), fh_env)[0]
    assert [f['path'] for f in evt['addedByGit']] == ['extra.txt']
    assert evt['modifiedFiles'] == 1


def test_existing_modified_list_is_not_duplicated(fh_env):
    """The journal-derived list is authoritative and already contains the file;
    re-adding it would render two rows for one file in the UI."""
    fh_env['diff'] = [{'path': 'already.py', 'action': 'modified'}]
    fh_env['tracked'] = {'already.py': {'last_writer_task_id': TASK_ID}}
    task = _task(modifiedFileList=[{'path': 'already.py', 'action': 'written'}])
    _run(task, fh_env)
    assert len(task['modifiedFileList']) == 1
    assert task['modifiedFileList'][0]['action'] == 'written', (
        'the authoritative journal entry was overwritten by the fh diff')


def test_rooted_existing_entry_dedups_against_unrooted_fh_entry(fh_env):
    """modifications.py records a `root` name; the fh diff may not know it.
    Without the unrooted alias the same file appears twice in the files bar."""
    fh_env['diff'] = [{'path': 'src/a.py', 'action': 'modified'}]
    fh_env['tracked'] = {'src/a.py': {'last_writer_task_id': TASK_ID}}
    task = _task(modifiedFileList=[
        {'path': 'src/a.py', 'action': 'written', 'root': 'primary'}])
    _run(task, fh_env)
    assert len(task['modifiedFileList']) == 1


def test_daemon_body_never_raises_on_internal_failure(fh_env, monkeypatch):
    """The body runs in a daemon thread: an escaping exception is invisible to
    the round and would silently lose the snapshot. It must log, not raise."""
    import types
    override = types.SimpleNamespace(
        is_enabled=lambda: True,
        get_last_snapshot_id=lambda p: (_ for _ in ()).throw(
            RuntimeError('store corrupt')),
    )
    monkeypatch.setitem(sys.modules, 'lib.file_history', override)
    import lib as _lib_pkg
    monkeypatch.setattr(_lib_pkg, 'file_history', override, raising=False)
    task = _task()
    commit_mod._run_commit_round_async(task, '/proj')   # must not raise
    assert fh_env['events'] == [], 'a corrupt store must not emit round_committed'
    assert 'snapshotId' not in task, 'a failed snapshot must not stamp the task'


def test_disabled_file_history_is_a_clean_noop(fh_env, monkeypatch):
    import types
    override = types.SimpleNamespace(is_enabled=lambda: False)
    monkeypatch.setitem(sys.modules, 'lib.file_history', override)
    import lib as _lib_pkg
    monkeypatch.setattr(_lib_pkg, 'file_history', override, raising=False)
    task = _task()
    events = _run(task, fh_env)
    assert events == [] and 'snapshotId' not in task


# ══════════════════════════════════════════════════════════════════
#  Turn-native projection fold — the files-changed card's only post-done path
# ══════════════════════════════════════════════════════════════════

def test_daemon_folds_file_list_into_the_turn_projection(fh_env, monkeypatch):
    """The turn-native UI reads the turn projection, not the legacy message —
    the daemon must land the derived list there or the files-changed card
    never renders (post-settlement frames are refused by the authority)."""
    import lib.turn_lifecycle as lifecycle
    folds = []
    monkeypatch.setattr(
        lifecycle, 'apply_commit_round_file_changes',
        lambda conv_id, turn_id, **kw: folds.append((conv_id, turn_id, kw)))
    fh_env['diff'] = [{'path': 'mine.py', 'action': 'modified'}]
    fh_env['tracked'] = {'mine.py': {'last_writer_task_id': TASK_ID}}
    task = _task(_turnId='turn-1')
    _run(task, fh_env)
    assert len(folds) == 1
    conv_id, turn_id, request = folds[0]
    assert (conv_id, turn_id) == ('conv-1', 'turn-1')
    assert request['files'] == [{'path': 'mine.py', 'action': 'modified'}]
    assert request['task_id'] == TASK_ID
    assert request['user_id'] == 1


def test_turn_projection_fold_requires_list_conv_and_turn(monkeypatch):
    """Missing any of the three → no fold at all (never a partial patch)."""
    import lib.turn_lifecycle as lifecycle
    calls = []
    monkeypatch.setattr(lifecycle, 'apply_commit_round_file_changes',
                        lambda *a, **kw: calls.append((a, kw)))
    commit_mod._patch_turn_projection_with_file_list(_task(_turnId='t'), {})
    commit_mod._patch_turn_projection_with_file_list(
        _task(convId='', _turnId='t'),
        {'modifiedFileList': [{'path': 'x.py'}]})
    commit_mod._patch_turn_projection_with_file_list(
        _task(), {'modifiedFileList': [{'path': 'x.py'}]})
    assert calls == []


def test_turn_projection_fold_failure_is_contained(monkeypatch, caplog):
    """The body runs in a daemon thread: a fold failure must log, not raise —
    the snapshot persist already happened."""
    import lib.turn_lifecycle as lifecycle
    monkeypatch.setattr(
        lifecycle, 'apply_commit_round_file_changes',
        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError('sidecar down')))
    with caplog.at_level('DEBUG', logger=commit_mod.__name__):
        commit_mod._patch_turn_projection_with_file_list(
            _task(_turnId='turn-1'),
            {'modifiedFileList': [{'path': 'x.py'}]})  # must not raise
    assert 'turn-projection file-changes fold failed: sidecar down' in caplog.text


# ══════════════════════════════════════════════════════════════════
#  Preference-consolidation daemon (_profile.py)
# ══════════════════════════════════════════════════════════════════

@pytest.fixture
def profile_spawn(monkeypatch):
    calls = []

    class _AlwaysAvailableSlot:
        def acquire(self, blocking=False):
            assert blocking is False
            return True

        def release(self):
            return None

    class _FakeThread:
        def __init__(self, **kw):
            calls.append(kw)

        def start(self):
            calls[-1]['started'] = True

    from lib.tasks_pkg.commit_round import _profile as prof
    monkeypatch.setattr(prof.threading, 'Thread', _FakeThread)
    monkeypatch.setattr(prof, '_PROFILE_CONSOLIDATION_SLOT',
                        _AlwaysAvailableSlot())
    return calls


def test_profile_spawn_gated_on_eligibility_and_clean_finish(profile_spawn):
    """Consolidation costs a cheap-LLM round trip; it must not run on an errored
    turn nor when the prefetch gate did not mark it eligible."""
    profile_mod._spawn_async_profile_consolidation(_task(), [])                      # not eligible
    profile_mod._spawn_async_profile_consolidation(
        _task(_profileConsolidateEligible=True, error='boom'), [])          # errored
    profile_mod._spawn_async_profile_consolidation(
        _task(id='', _profileConsolidateEligible=True), [])                 # no id
    assert profile_spawn == []

    profile_mod._spawn_async_profile_consolidation(
        _task(_profileConsolidateEligible=True), [])
    assert len(profile_spawn) == 1 and profile_spawn[0]['daemon'] is True


def test_profile_spawn_is_bounded_when_worker_busy(monkeypatch):
    """Concurrent completed turns cannot create an unbounded learner burst."""
    class _BusySlot:
        def acquire(self, blocking=False):
            assert blocking is False
            return False

        def release(self):
            raise AssertionError('an unacquired slot must not be released')

    monkeypatch.setattr(profile_mod, '_PROFILE_CONSOLIDATION_SLOT', _BusySlot())
    monkeypatch.setattr(
        profile_mod.threading, 'Thread',
        lambda **kwargs: (_ for _ in ()).throw(
            AssertionError('busy learner must not create a thread')),
    )

    profile_mod._spawn_async_profile_consolidation(
        _task(_profileConsolidateEligible=True), [])


def test_terminal_finalizer_schedules_profile_consolidation():
    """Guard the production call site; a helper alone does not auto-learn."""
    import ast
    import inspect
    from lib.tasks_pkg.orchestrator import _finalize

    finalizer = ast.parse(
        inspect.getsource(_finalize._finalize_and_emit_done)).body[0]
    calls = {
        node.func.id
        for node in ast.walk(finalizer)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert '_spawn_async_profile_consolidation' in calls


@pytest.fixture
def prof_env(monkeypatch):
    """Stub the consolidation LLM pass + capture emitted events."""
    import types
    env = {'learned': [], 'events': [], 'patched': None}

    monkeypatch.setitem(
        sys.modules, 'lib.memory.profile_consolidate',
        types.SimpleNamespace(
            run_profile_consolidation=lambda msgs, task=None: list(env['learned'])))
    monkeypatch.setattr(profile_mod, 'append_event',
                        lambda task, evt: env['events'].append(evt))
    monkeypatch.setattr(profile_mod, '_patch_turn_with_prefs',
                        lambda task, learned: env.update(patched=learned))
    return env


def test_each_learned_preference_gets_its_own_event(prof_env):
    prof_env['learned'] = [
        {'kind': 'style', 'summary': 'prefers tables', 'id': 'p1'},
        {'kind': 'tooling', 'summary': 'prefers ripgrep', 'id': 'p2', 'pending': True},
    ]
    task = _task()
    profile_mod._run_profile_consolidation_async(task, [])
    assert [e.get('summary') for e in prof_env['events']] == [
        'prefers tables', 'prefers ripgrep']
    assert prof_env['events'][1].get('pending') is True
    assert task['_preferencesLearned'] == prof_env['learned']
    assert prof_env['patched'] == prof_env['learned']


def test_nothing_learned_emits_nothing(prof_env):
    prof_env['learned'] = []
    task = _task()
    profile_mod._run_profile_consolidation_async(task, [])
    assert prof_env['events'] == []
    assert '_preferencesLearned' not in task
    assert prof_env['patched'] is None


def test_consolidation_failure_is_contained(monkeypatch, prof_env):
    """A cheap-LLM failure must not escape the daemon thread nor mark the task."""
    import types
    monkeypatch.setitem(
        sys.modules, 'lib.memory.profile_consolidate',
        types.SimpleNamespace(
            run_profile_consolidation=lambda msgs, task=None: (
                _ for _ in ()).throw(RuntimeError('LLM down'))))
    task = _task()
    profile_mod._run_profile_consolidation_async(task, [])   # must not raise
    assert '_preferencesLearned' not in task


def test_emit_failure_does_not_abort_persistence(monkeypatch, prof_env):
    """Live SSE delivery is best-effort; the DB patch is what survives reload,
    so a failing emit must NOT skip it."""
    prof_env['learned'] = [{'kind': 'style', 'summary': 's', 'id': 'p1'}]
    monkeypatch.setattr(profile_mod, 'append_event',
                        lambda t, e: (_ for _ in ()).throw(RuntimeError('no sse')))
    profile_mod._run_profile_consolidation_async(_task(), [])
    assert prof_env['patched'] == prof_env['learned']


def test_prefs_patch_updates_turn_provenance_with_revision_cas(monkeypatch):
    import lib.turn_lifecycle as lifecycle

    learned = [{'kind': 'added', 'summary': 'Prefer stable IDs', 'id': 'p1'}]
    updates = []
    monkeypatch.setattr(lifecycle, 'get_turn', lambda conv_id, turn_id, **kw: {
        'turnId': turn_id,
        'projectionRevision': 7,
        'projection': {
            'content': 'answer',
            'toolRounds': [{'toolName': 'read_files', 'toolContent': 'kept'}],
            'segments': [{'type': 'tool', 'content': 'kept'}],
            'provenance': {
                'blockId': 'provenance',
                'memoryPrefetch': {'phase': 'done', 'selected': 1},
            },
        },
    })
    monkeypatch.setattr(
        lifecycle,
        '_task_projection',
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError('preference patch must not refold task structure')),
    )
    monkeypatch.setattr(
        lifecycle,
        'update_turn_projection',
        lambda conv_id, turn_id, **kw: updates.append(
            (conv_id, turn_id, kw)) or {'ok': True},
    )

    profile_mod._patch_turn_with_prefs(_task(
        _turnId='turn-a', _userId=9, content='answer',
        _preferencesLearned=learned,
    ), learned)

    assert len(updates) == 1
    conv_id, turn_id, request = updates[0]
    assert (conv_id, turn_id) == ('conv-1', 'turn-a')
    assert request['expected_projection_revision'] == 7
    assert request['user_id'] == 9
    assert request['projection']['content'] == 'answer'
    assert request['projection']['toolRounds'] == [
        {'toolName': 'read_files', 'toolContent': 'kept'},
    ]
    assert request['projection']['segments'] == [
        {'type': 'tool', 'content': 'kept'},
    ]
    assert request['projection']['provenance'] == {
        'blockId': 'provenance',
        'memoryPrefetch': {'phase': 'done', 'selected': 1},
        'preferencesLearned': learned,
    }


def test_prefs_patch_requires_conv_task_and_learned(monkeypatch):
    import lib.turn_lifecycle as lifecycle
    monkeypatch.setattr(
        lifecycle, 'get_turn',
        lambda *a, **kw: (_ for _ in ()).throw(
            AssertionError('must not touch the turn authority')))
    profile_mod._patch_turn_with_prefs(_task(convId=''), [{'id': 'p'}])
    profile_mod._patch_turn_with_prefs(_task(id=''), [{'id': 'p'}])
    profile_mod._patch_turn_with_prefs(_task(), [])


def test_no_legacy_taskid_projection_bridge_remains():
    """Post-settlement enrichment goes through the _turnId-keyed CAS seam only.

    Turn projections strip identity keys (``_taskId``) at every persistence
    path (``normalize_projection_document``), so a bridge that locates the row
    by ``projection._taskId`` can never match — it only logged
    ``[Store] no turn projection tagged`` and silently dropped the payload
    (2026-08-26: every commit round; fileChanges/gitSha never landed).
    """
    import inspect

    import lib.tasks_pkg.persistence_store as persistence_store
    from lib.protocols import ConversationStore

    for module in (commit_mod, profile_mod):
        source = inspect.getsource(module)
        assert 'patch_message_fields_by_task' not in source
        assert '_patch_assistant_message' not in source
    assert not hasattr(
        persistence_store.DefaultConversationStore,
        'patch_message_fields_by_task')
    assert 'patch_message_fields_by_task' not in inspect.getsource(
        ConversationStore)


def test_package_is_namespace_only():
    """Concrete owner modules, rather than package re-exports, are authoritative."""
    import lib.tasks_pkg.commit_round as package

    for old_export in (
        '_spawn_async_commit_round',
        '_spawn_async_profile_consolidation',
        'append_event',
    ):
        assert not hasattr(package, old_export)
