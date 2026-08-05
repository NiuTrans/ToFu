"""Round-commit daemon: attribution filter, spawn gates, message patching.

WHY THIS FILE EXISTS
────────────────────
`commit_round/_commit.py` + `_profile.py` are the per-round LANDING point: they
snapshot the file-history, decide which side-channel file edits belong to THIS
round, emit `round_committed`, and patch the conversation's assistant message
after `persist_task_result` has already run. A regression loses task results
that nothing else can reconstruct.

Measured before this file existed: `_commit.py` **6%**, `_profile.py` **13%** —
the package was reachable only incidentally.

DESIGN: why no real threads
    Both modules are split into `_spawn_*` (starts a daemon thread) and
    `_run_*_async` (the thread BODY). Their docstrings state the body resolves
    its callees THROUGH the facade (`_facade.append_event`,
    `_facade._patch_assistant_message_with_prefs`) precisely so a test can steer
    them. So the tests here drive the BODY synchronously and assert the
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

import lib.tasks_pkg.commit_round as cr
from lib.tasks_pkg.commit_round import _commit as commit_mod

pytestmark = pytest.mark.unit

TASK_ID = 'task-abcdef123456'
OTHER_TASK = 'task-of-a-concurrent-conversation'


def _task(**over):
    t = {'id': TASK_ID, 'convId': 'conv-1', 'toolRounds': []}
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
    cr._spawn_async_commit_round(_task(), False, '/proj')          # disabled
    cr._spawn_async_commit_round(_task(), True, None)              # no path
    cr._spawn_async_commit_round(_task(id=''), True, '/proj')      # no task id
    assert spawned == []

    cr._spawn_async_commit_round(_task(), True, '/proj')
    assert len(spawned) == 1 and spawned[0].get('started') is True


def test_commit_spawn_thread_is_a_daemon(spawned):
    """A non-daemon thread would keep the process alive on shutdown."""
    cr._spawn_async_commit_round(_task(), True, '/proj')
    assert spawned[0]['daemon'] is True


def test_commit_spawn_failure_is_swallowed(monkeypatch):
    """Failing to spawn must not break the round — the snapshot is best-effort.

    This runs on the loop-exit → `done` path; raising here would turn a
    completed round into a failed one over a missing snapshot.
    """
    def boom(**kw):
        raise RuntimeError('cannot start thread')

    monkeypatch.setattr(commit_mod.threading, 'Thread', boom)
    cr._spawn_async_commit_round(_task(), True, '/proj')   # must not raise


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
    monkeypatch.setattr(cr, 'append_event',
                        lambda task, evt: env['events'].append(evt))
    monkeypatch.setattr(commit_mod, 'append_event',
                        lambda task, evt: env['events'].append(evt))
    monkeypatch.setattr(cr, '_patch_assistant_message_with_git',
                        lambda task, evt: None)
    monkeypatch.setattr(commit_mod, '_patch_assistant_message_with_git',
                        lambda task, evt: None)
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
#  Assistant-message patching — survives the SSE reader closing
# ══════════════════════════════════════════════════════════════════

@pytest.fixture
def store(monkeypatch):
    """Fake conversation store; returns the dict holding loaded/saved messages.

    Mirrors the NARROW-WRITE contract the daemons now use: they no longer
    read-modify-write the whole transcript (that erased rows a concurrent
    autopilot append had just committed), they patch the fields of the ONE
    message tagged with their own task id. The fake reproduces that, including
    the deliberate absence of any positional fallback.
    """
    import types
    state = {'messages': [], 'saved': None}

    def _patch(cid, task_id, fields, *, max_attempts=5):
        msgs = [dict(m) for m in state['messages']]
        for i in range(len(msgs) - 1, -1, -1):
            m = msgs[i]
            if m.get('role') == 'assistant' and m.get('_taskId') == task_id:
                m.update(fields)
                state['saved'] = msgs
                return True
        return False

    fake = types.SimpleNamespace(patch_message_fields_by_task=_patch)
    monkeypatch.setitem(
        sys.modules, 'lib.agent_core.store',
        types.SimpleNamespace(get_conversation_store=lambda: fake))
    return state


def test_patch_targets_the_message_tagged_with_this_task(store):
    """Prefer the _taskId match over "last assistant" — with a follow-up task
    already appended, the tail is someone else's message."""
    store['messages'] = [
        {'role': 'user', 'content': 'q'},
        {'role': 'assistant', 'content': 'mine', '_taskId': TASK_ID},
        {'role': 'assistant', 'content': 'later', '_taskId': 'task-newer'},
    ]
    cr._patch_assistant_message_with_git(
        _task(), {'gitSha': 'snap-9', 'snapshotId': 'snap-9'})
    assert store['saved'][1]['_snapshotId'] == 'snap-9'
    assert '_snapshotId' not in store['saved'][2]


def test_patch_writes_nothing_when_no_message_carries_this_task(store):
    """No ``_taskId`` match → write NOTHING; never guess the last assistant.

    This asserts the INVERSE of what it once did. The old "fall back to the last
    assistant message" branch is the blob-clobber bug wearing a quieter mask:
    these daemons run AFTER the turn settled, so by the time they fire the tail
    may belong to a DIFFERENT task (a follow-up, an autopilot VU turn) and the
    snapshot id gets stamped onto someone else's turn. An untagged message means
    the row was compacted away or rebuilt — the correct action is to skip and
    say so in the log, not to guess.
    """
    store['messages'] = [{'role': 'user', 'content': 'q'},
                         {'role': 'assistant', 'content': 'a'}]
    cr._patch_assistant_message_with_git(
        _task(), {'gitSha': 'snap-9', 'snapshotId': 'snap-9'})
    assert store['saved'] is None, (
        'the snapshot was stamped onto an untagged assistant message — under '
        'concurrency that is another task\'s turn')


def test_patch_requires_conv_task_and_sha(store):
    """Missing any of the three → no write at all (never a partial patch)."""
    evt = {'gitSha': 'snap-9'}
    cr._patch_assistant_message_with_git(_task(convId=''), evt)
    cr._patch_assistant_message_with_git(_task(id=''), evt)
    cr._patch_assistant_message_with_git(_task(), {})
    assert store['saved'] is None


def test_patch_is_a_noop_when_conversation_is_gone(monkeypatch):
    """A deleted conversation must not raise from the post-done daemon."""
    import types
    calls = []

    def _gone(cid, tid, fields, **kw):
        calls.append((cid, tid))
        return False

    fake = types.SimpleNamespace(patch_message_fields_by_task=_gone)
    monkeypatch.setitem(
        sys.modules, 'lib.agent_core.store',
        types.SimpleNamespace(get_conversation_store=lambda: fake))
    cr._patch_assistant_message_with_git(_task(), {'gitSha': 's'})
    # The tagged-message lookup was attempted (narrow write, no positional
    # fallback) and its False verdict was accepted without raising.
    assert calls == [('conv-1', TASK_ID)]


def test_patch_carries_modified_list_onto_the_message(store):
    """Needed so a RELOAD (SSE long gone) still shows the files-changed bar."""
    store['messages'] = [{'role': 'assistant', 'content': 'a', '_taskId': TASK_ID}]
    cr._patch_assistant_message_with_git(_task(), {
        'gitSha': 's', 'snapshotId': 's',
        'modifiedFileList': [{'path': 'x.py', 'action': 'written'}],
        'modifiedFiles': 1,
    })
    assert store['saved'][0]['modifiedFileList'][0]['path'] == 'x.py'
    assert store['saved'][0]['modifiedFiles'] == 1


# ══════════════════════════════════════════════════════════════════
#  Preference-consolidation daemon (_profile.py)
# ══════════════════════════════════════════════════════════════════

@pytest.fixture
def profile_spawn(monkeypatch):
    calls = []

    class _FakeThread:
        def __init__(self, **kw):
            calls.append(kw)

        def start(self):
            calls[-1]['started'] = True

    from lib.tasks_pkg.commit_round import _profile as prof
    monkeypatch.setattr(prof.threading, 'Thread', _FakeThread)
    return calls


def test_profile_spawn_gated_on_eligibility_and_clean_finish(profile_spawn):
    """Consolidation costs a cheap-LLM round trip; it must not run on an errored
    turn nor when the prefetch gate did not mark it eligible."""
    cr._spawn_async_profile_consolidation(_task(), [])                      # not eligible
    cr._spawn_async_profile_consolidation(
        _task(_profileConsolidateEligible=True, error='boom'), [])          # errored
    cr._spawn_async_profile_consolidation(
        _task(id='', _profileConsolidateEligible=True), [])                 # no id
    assert profile_spawn == []

    cr._spawn_async_profile_consolidation(
        _task(_profileConsolidateEligible=True), [])
    assert len(profile_spawn) == 1 and profile_spawn[0]['daemon'] is True


@pytest.fixture
def prof_env(monkeypatch):
    """Stub the consolidation LLM pass + capture emitted events."""
    import types
    env = {'learned': [], 'events': [], 'patched': None}

    monkeypatch.setitem(
        sys.modules, 'lib.memory.profile_consolidate',
        types.SimpleNamespace(
            run_profile_consolidation=lambda msgs, task=None: list(env['learned'])))
    monkeypatch.setattr(cr, 'append_event',
                        lambda task, evt: env['events'].append(evt))
    monkeypatch.setattr(cr, '_patch_assistant_message_with_prefs',
                        lambda task, learned: env.update(patched=learned))
    return env


def test_each_learned_preference_gets_its_own_event(prof_env):
    prof_env['learned'] = [
        {'kind': 'style', 'summary': 'prefers tables', 'id': 'p1'},
        {'kind': 'tooling', 'summary': 'prefers ripgrep', 'id': 'p2', 'pending': True},
    ]
    task = _task()
    cr._run_profile_consolidation_async(task, [])
    assert [e.get('summary') for e in prof_env['events']] == [
        'prefers tables', 'prefers ripgrep']
    assert prof_env['events'][1].get('pending') is True
    assert task['_preferencesLearned'] == prof_env['learned']
    assert prof_env['patched'] == prof_env['learned']


def test_nothing_learned_emits_nothing(prof_env):
    prof_env['learned'] = []
    task = _task()
    cr._run_profile_consolidation_async(task, [])
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
    cr._run_profile_consolidation_async(task, [])   # must not raise
    assert '_preferencesLearned' not in task


def test_emit_failure_does_not_abort_persistence(monkeypatch, prof_env):
    """Live SSE delivery is best-effort; the DB patch is what survives reload,
    so a failing emit must NOT skip it."""
    prof_env['learned'] = [{'kind': 'style', 'summary': 's', 'id': 'p1'}]
    monkeypatch.setattr(cr, 'append_event',
                        lambda t, e: (_ for _ in ()).throw(RuntimeError('no sse')))
    cr._run_profile_consolidation_async(_task(), [])
    assert prof_env['patched'] == prof_env['learned']


def test_prefs_patch_targets_the_tagged_message(store):
    store['messages'] = [
        {'role': 'assistant', 'content': 'mine', '_taskId': TASK_ID},
        {'role': 'assistant', 'content': 'other', '_taskId': 'task-newer'},
    ]
    learned = [{'kind': 'style', 'summary': 's', 'id': 'p1'}]
    cr._patch_assistant_message_with_prefs(_task(), learned)
    assert store['saved'][0]['_preferencesLearned'] == learned
    assert '_preferencesLearned' not in store['saved'][1]


def test_prefs_patch_requires_conv_task_and_learned(store):
    cr._patch_assistant_message_with_prefs(_task(convId=''), [{'id': 'p'}])
    cr._patch_assistant_message_with_prefs(_task(id=''), [{'id': 'p'}])
    cr._patch_assistant_message_with_prefs(_task(), [])
    assert store['saved'] is None


# ══════════════════════════════════════════════════════════════════
#  Facade contract — the patch seam these daemons depend on
# ══════════════════════════════════════════════════════════════════

def test_facade_reexports_the_documented_surface():
    """The package docstring promises `commit_round.X` keeps resolving after the
    monolith split, and the daemon bodies resolve callees THROUGH it so tests
    can steer them. A dropped name breaks that seam at runtime, not at import."""
    for name in cr.__all__:
        assert hasattr(cr, name), f'facade lost {name}'
    for name in ('append_event', 'EventType', 'build_event'):
        assert name in cr.__all__, f'{name} must stay patchable at facade scope'
