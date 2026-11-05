"""An accepted attempt lists its memory corpus at acceptance, not in startup.

The prefetch corpus listing depends only on the attempt's roots, so an accepted
ordinary attempt whose configuration opens the memory-prefetch gate starts it
next to its input translation, and ``startup_memory`` ranks that listing
exactly as it ranks an inline one. The listing starts after acceptance, so
every memory written before acceptance is visible. Attempts that cannot
prefetch never list; a stopped attempt, or one whose roots moved, releases it
unused.
"""

from __future__ import annotations

from concurrent.futures import Future
import os
import threading
import time
from pathlib import Path

import pytest


pytestmark = pytest.mark.unit

_WAIT_SECONDS = 10.0
_QUERY = 'fix ParserState in lib/parser/state.py'


def _run_now(fn, *args):
    future = Future()
    try:
        future.set_result(fn(*args))
    except BaseException as error:  # noqa: BLE001 - the Future carries it
        future.set_exception(error)
    return future


def _messages(text=_QUERY):
    return [{'role': 'user', 'content': text}]


def _write_memory(directory: Path, memory_id: str, description: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f'{memory_id}.md'
    path.write_text(
        '---\n'
        f'name: {memory_id}\n'
        f'description: {description}\n'
        'tags: [parser]\n'
        '---\n\n'
        f'body of {memory_id}\n',
        encoding='utf-8',
    )
    return path


@pytest.fixture()
def project(tmp_path, monkeypatch):
    import lib.memory.storage._dirs as storage_dirs
    import lib.memory.storage._files as memory_files

    data_dir = tmp_path / 'data'
    root = tmp_path / 'project'
    (root / '.tofu' / 'memories').mkdir(parents=True)
    monkeypatch.setenv('TOFU_DATA_DIR', str(data_dir))
    monkeypatch.setattr(storage_dirs, '_server_data_dir', lambda: str(data_dir))
    storage_dirs._migrated_roots.clear()
    storage_dirs._migrated_roots.add(str(root))
    storage_dirs._server_store_migrated = True
    memory_files._metadata_cache.clear()
    yield root
    memory_files._metadata_cache.clear()
    storage_dirs._migrated_roots.clear()
    storage_dirs._server_store_migrated = False


@pytest.fixture()
def submissions(monkeypatch):
    """Run early listings synchronously and record every submission."""
    from lib.tasks_pkg.context_composer._provider_executor import (
        context_provider_executor,
    )

    submitted = []

    def submit(fn, *args):
        submitted.append(fn)
        return _run_now(fn, *args)

    monkeypatch.setattr(context_provider_executor, 'submit', submit)
    return submitted


def _stub_acceptance(monkeypatch, order):
    """Real attempt acceptance over stubbed storage, registry and executor."""
    import lib.conversation_sync.task_start as task_start

    monkeypatch.setattr(task_start, '_throttled_cleanup_old_tasks', lambda: None)
    monkeypatch.setattr(
        'lib.tasks_pkg.coordinator_policy.pin_coordinator_request_policy',
        lambda conversation_id, config, owner_user_id: config)
    monkeypatch.setattr(
        'lib.turn_lifecycle.build_api_messages',
        lambda *args, **kwargs: order.append(('context',)) or _messages())
    task = {'id': 'task-early', 'convId': 'conv-early', 'config': {},
            'status': 'pending'}
    monkeypatch.setattr(task_start, 'create_task', lambda *args, **kwargs: task)
    monkeypatch.setattr('lib.tasks_pkg.manager.discard_task',
                        lambda task_id, conv_id=None: None)
    return task_start, task


def _config(**fields):
    return {'model': 'm', '_turnOwnerUserId': 1, '_turnId': 'turn-e',
            '_attemptId': 'attempt-e', **fields}


def _accept(monkeypatch, config, order):
    """Accept an ordinary attempt through to its worker submission."""
    task_start, task = _stub_acceptance(monkeypatch, order)
    monkeypatch.setattr(
        'lib.orchestration.chat_flow_runner.resolve_chat_flow_entry',
        lambda cfg: None)
    spawned = []
    monkeypatch.setattr('lib.tasks_pkg.spawn.spawn_task', spawned.append)

    started = task_start.start_conversation_attempt_executor(
        'conv-early', _config(**config))

    assert started == ('task-early', None)
    assert spawned == [task]
    return task


# ── Acceptance ────────────────────────────────────────────────────────

def test_accepted_attempt_lists_its_corpus_before_model_context(
        monkeypatch, submissions):
    order = []
    monkeypatch.setattr(
        'lib.memory.storage.get_eligible_memories',
        lambda project_path, **kwargs: order.append(
            ('list', project_path, kwargs)) or [])

    task = _accept(monkeypatch, {
        'projectPath': '/proj', 'projectPaths': ['/proj', '/extra', '']}, order)

    assert order == [
        ('list', '/proj', {'extra_paths': ['/extra'], 'include_body': False,
                           'record_view': 'retrieval'}),
        ('context',),
    ]
    assert len(submissions) == 1
    assert task['_earlyMemoryListing'].roots == ('/proj', ('/extra',))


def _pending_submissions(monkeypatch):
    from lib.tasks_pkg.context_composer._provider_executor import (
        context_provider_executor,
    )

    pending = Future()
    monkeypatch.setattr(context_provider_executor, 'submit',
                        lambda fn, *args: pending)
    return pending


def test_acceptance_without_a_task_releases_its_listing(monkeypatch):
    pending = _pending_submissions(monkeypatch)
    task_start, _task = _stub_acceptance(monkeypatch, [])
    monkeypatch.setattr(
        'lib.turn_lifecycle.build_api_messages', lambda *args, **kwargs: None)

    started = task_start.start_conversation_attempt_executor(
        'conv-early', _config())

    assert started == (None, 'conversation_not_found')
    assert pending.cancelled()


def test_discarded_unstarted_attempt_releases_its_listing(monkeypatch):
    pending = _pending_submissions(monkeypatch)
    task_start, task = _stub_acceptance(monkeypatch, [])

    def refuse(*args):
        raise RuntimeError('preparation refused')

    monkeypatch.setattr(task_start, '_prepare_unbound_task_dispatch', refuse)
    with pytest.raises(RuntimeError, match='preparation refused'):
        task_start.start_conversation_attempt_executor('conv-early', _config())

    assert pending.cancelled()
    assert '_earlyMemoryListing' not in task


@pytest.mark.parametrize('config', [
    {'memoryEnabled': False},
    {'profile': 'minimal'},
    {'toolHistory': [{
        'toolCalls': [{'id': 'call-1', 'name': 'read_files', 'arguments': '{}'}],
        'toolResults': [{'tool_call_id': 'call-1', 'content': 'x'}]}]},
    {'flowBuiltin': 'review'},
    {'autopilot': True},
    {'_projectDispatchAdmission': {'assignmentId': 'assignment-1'}},
], ids=['memory-off', 'profile-memory-off', 'continue-tool-history', 'flow',
        'goal-mode', 'project-dispatch'])
def test_attempts_that_cannot_prefetch_never_list(monkeypatch, submissions,
                                                  config):
    from lib.tasks_pkg.memory_listing import start_early_memory_listing

    monkeypatch.setattr(
        'lib.memory.storage.get_eligible_memories',
        lambda *args, **kwargs: pytest.fail('a non-prefetching attempt listed'))

    assert start_early_memory_listing({'model': 'm', 'projectPath': '/proj',
                                       **config}) is None
    assert submissions == []


def test_disabled_prefetch_never_lists(monkeypatch, submissions):
    from lib.tasks_pkg.memory_listing import start_early_memory_listing

    monkeypatch.setattr('lib.memory.prefetch.PREFETCH_ENABLED', False)
    assert start_early_memory_listing({'projectPath': '/proj'}) is None
    assert submissions == []


def test_a_listing_that_cannot_start_never_fails_acceptance(monkeypatch):
    from lib.tasks_pkg.context_composer._provider_executor import (
        context_provider_executor,
    )

    def broken_submit(fn, *args):
        raise RuntimeError('submission refused')

    monkeypatch.setattr(context_provider_executor, 'submit', broken_submit)
    task = _accept(monkeypatch, {'projectPath': '/proj'}, [])

    assert '_earlyMemoryListing' not in task


def test_memory_disabled_acceptance_never_lists(monkeypatch, submissions):
    order = []
    monkeypatch.setattr(
        'lib.memory.storage.get_eligible_memories',
        lambda *args, **kwargs: pytest.fail('a memory-disabled attempt listed'))

    task = _accept(monkeypatch, {'projectPath': '/proj',
                                 'memoryEnabled': False}, order)

    assert order == [('context',)]
    assert submissions == []
    assert '_earlyMemoryListing' not in task


@pytest.mark.parametrize('config', [
    {},
    {'projectPath': '/proj'},
    {'projectPath': '/proj', 'projectPaths': ['/proj', '/b', '/b', '', '/c']},
    {'projectPaths': ['/a', '/b']},
    {'projectPath': '/proj', 'chatMode': 'chat'},
    {'projectPath': '/proj', 'profile': 'coding'},
])
def test_acceptance_lists_exactly_the_roots_startup_memory_ranks(
        monkeypatch, config):
    import lib.memory.prefetch as prefetch
    import lib.tasks_pkg.orchestrator._memory_prefetch as seam
    from lib.agent_core.profiles import apply_profile
    from lib.tasks_pkg.model_config import _resolve_model_config

    effective = apply_profile(config)
    mcfg = _resolve_model_config(effective, 'roots-parity')
    ranked = []
    monkeypatch.setattr(prefetch, 'run_memory_prefetch',
                        lambda messages, **kwargs: ranked.append(
                            (kwargs['project_path'], tuple(kwargs['extra_paths']))))
    seam.maybe_run_memory_prefetch(
        task={'id': 'roots'}, cfg=effective, messages=_messages(),
        tool_list=[], project_path=mcfg['project_path'],
        project_enabled=mcfg['project_enabled'],
        memory_enabled=mcfg['memory_enabled'], has_real_tools=True,
        injected_tool_calls=0)

    roots = seam.accepted_memory_prefetch_roots(config)
    assert roots is not None
    assert ranked == [(roots[0], tuple(roots[1]))]


# ── Freshness ─────────────────────────────────────────────────────────

class _BlockedProjectScan:
    """The first scan of the project store snapshots, then waits."""

    def __init__(self, monkeypatch, project_root: Path):
        import lib.memory.storage._files as memory_files

        self.directory = os.path.normpath(
            str(project_root / '.tofu' / 'memories'))
        self.snapshot_taken = threading.Event()
        self.release = threading.Event()
        self._scans = 0
        self._lock = threading.Lock()
        self._original = memory_files._sorted_visible_dir_entries
        monkeypatch.setattr(memory_files, '_sorted_visible_dir_entries',
                            self._entries)

    def _entries(self, dirpath):
        entries = self._original(dirpath)
        if os.path.normpath(str(dirpath)) != self.directory:
            return entries
        with self._lock:
            self._scans += 1
            first = self._scans == 1
        if first:
            self.snapshot_taken.set()
            assert self.release.wait(_WAIT_SECONDS)
        return entries


def _wait_for_queued_callers(count):
    import lib.memory.storage._crud as crud

    deadline = time.monotonic() + _WAIT_SECONDS
    while crud._memory_listing_reads.snapshot()['queuedCallers'] != count:
        assert time.monotonic() < deadline, f'{count} callers never queued'
        time.sleep(0.001)


def test_memory_written_before_acceptance_is_listed_even_behind_a_running_scan(
        project, monkeypatch):
    from lib.memory.storage import create_memory, get_eligible_memories
    from lib.tasks_pkg.memory_listing import start_early_memory_listing

    memories = project / '.tofu' / 'memories'
    _write_memory(memories, 'existing-note', 'an older parser note')
    scan = _BlockedProjectScan(monkeypatch, project)
    older = {}
    other_task = threading.Thread(target=lambda: older.update(
        rows=get_eligible_memories(str(project), include_body=False,
                                   record_view='retrieval')), daemon=True)
    other_task.start()
    assert scan.snapshot_taken.wait(_WAIT_SECONDS)

    written = create_memory(
        name='Written Before Acceptance',
        description='another task wrote this before the attempt was accepted',
        body='durable body', scope='project', project_path=str(project))
    listing = start_early_memory_listing({'projectPath': str(project)})
    _wait_for_queued_callers(1)
    scan.release.set()
    other_task.join(_WAIT_SECONDS)

    rows = listing.take(str(project), [])
    assert [row['id'] for row in older['rows']] == ['existing-note']
    assert sorted(row['id'] for row in rows) == sorted(
        ['existing-note', written['id']])


# ── Consumption ───────────────────────────────────────────────────────

def _prefetch(project_root, *, corpus_listing=None, extra_paths=None):
    from lib.memory.prefetch import run_memory_prefetch

    events = []
    task = {'id': 'early-consumer', 'convId': 'conv-early'}
    selected = run_memory_prefetch(
        _messages(), str(project_root), task=task, emit_event=events.append,
        extra_paths=extra_paths, corpus_listing=corpus_listing)
    return selected, task, events


def _stable(event):
    return {key: value for key, value in event.items()
            if key not in ('total_ms', 'emittedAt')}


def test_startup_memory_ranks_an_early_listing_exactly_like_an_inline_one(
        project):
    from lib.tasks_pkg.memory_listing import start_early_memory_listing

    memories = project / '.tofu' / 'memories'
    _write_memory(memories, 'parser-state-rollback',
                  'ParserState rollback rule in lib/parser/state.py')
    _write_memory(memories, 'parser-format', 'ParserState formatting rule')
    _write_memory(memories, 'css-palette', 'CSS palette and typography')

    inline = _prefetch(project)
    listing = start_early_memory_listing({'projectPath': str(project)})
    early = _prefetch(project, corpus_listing=listing)

    assert [row['id'] for row in inline[0]] == ['parser-state-rollback',
                                                 'parser-format']
    assert early[0] == inline[0]
    assert early[1]['_prefetchedMemories'] == inline[1]['_prefetchedMemories']
    assert ([_stable(event) for event in early[2]]
            == [_stable(event) for event in inline[2]])


def test_listing_for_other_roots_is_not_used(monkeypatch):
    from lib.memory.prefetch import PrefetchCorpusListing

    listed = []

    def eligible(project_path, **kwargs):
        listed.append(project_path)
        return [{'id': f'note-{project_path}', 'name': 'ParserState rule',
                 'description': 'ParserState rule in lib/parser/state.py',
                 'tags': [], 'enabled': True, 'eligible': True,
                 'is_package': False, 'scope': 'project'}]

    monkeypatch.setattr('lib.memory.storage.get_eligible_memories', eligible)
    monkeypatch.setattr('lib.memory.storage.load_eligible_memories',
                        lambda ids, *args, **kwargs: [])
    listing = PrefetchCorpusListing('/canonical', [], submit=_run_now)

    selected, task, _events = _prefetch('/worker-checkout',
                                        corpus_listing=listing)

    assert listed == ['/canonical', '/worker-checkout']
    assert task['_memoryPrefetch']['phase'] == 'done'
    assert selected == []  # hydration found nothing; only the roots matter


def test_failed_early_listing_fails_the_prefetch_like_an_inline_listing(
        monkeypatch):
    from lib.memory.prefetch import PrefetchCorpusListing

    def broken(*args, **kwargs):
        raise OSError('memory store unavailable')

    monkeypatch.setattr('lib.memory.storage.get_eligible_memories', broken)
    _selected, inline, _events = _prefetch('/proj')
    listing = PrefetchCorpusListing('/proj', [], submit=_run_now)
    selected, early, _events = _prefetch('/proj', corpus_listing=listing)

    assert selected == []
    assert early['_memoryPrefetch'] == inline['_memoryPrefetch'] == {
        'phase': 'failed', 'reason': 'load_error:memory store unavailable'}


def test_unscheduled_early_listing_is_listed_inline(monkeypatch):
    from lib.memory.prefetch import PrefetchCorpusListing

    listed = []
    monkeypatch.setattr(
        'lib.memory.storage.get_eligible_memories',
        lambda project_path, **kwargs: listed.append(project_path) or [])

    def refuse(fn, *args):
        future = Future()
        future.set_exception(RuntimeError('executor closed'))
        return future

    listing = PrefetchCorpusListing('/proj', [], submit=refuse)
    _selected, task, _events = _prefetch('/proj', corpus_listing=listing)

    assert listed == ['/proj']
    assert task['_memoryPrefetch'] == {'phase': 'skipped',
                                       'reason': 'no_memories'}


class _RecordingListing:
    roots = ('/p', ())
    listing_ms = 0.0

    def __init__(self):
        self.taken = []
        self.released = 0

    def take(self, project_path, extra_paths):
        self.taken.append((project_path, tuple(extra_paths or ())))
        return []

    def release(self):
        self.released += 1


@pytest.mark.parametrize('gate', [
    {},
    {'memory_enabled': False},
    {'has_real_tools': False},
    {'injected_tool_calls': 1},
])
def test_startup_memory_owns_the_listing_whether_or_not_it_ranks(
        monkeypatch, gate):
    import lib.tasks_pkg.orchestrator._memory_prefetch as seam

    monkeypatch.setattr(seam, 'append_event', lambda task, event: None)
    listing = _RecordingListing()
    task = {'id': 'owner', 'events': [], 'events_lock': threading.Lock(),
            '_earlyMemoryListing': listing}
    seam.maybe_run_memory_prefetch(**{
        'task': task, 'cfg': {}, 'messages': _messages(), 'tool_list': [],
        'project_path': '/p', 'project_enabled': True, 'memory_enabled': True,
        'has_real_tools': True, 'injected_tool_calls': 0, **gate})

    assert '_earlyMemoryListing' not in task
    assert listing.released == 1
    assert listing.taken == ([('/p', ())] if not gate else [])


# ── Release ───────────────────────────────────────────────────────────

def test_stopped_attempt_releases_its_listing_without_prefetching(monkeypatch):
    import lib.tasks_pkg.orchestrator._run as run_mod
    from lib.log import clear_log_context, set_req_id

    monkeypatch.setattr(
        'lib.memory.prefetch.run_memory_prefetch',
        lambda *args, **kwargs: pytest.fail('a stopped attempt ran prefetch'))
    for name, value in (
            ('check_autopilot_kick', lambda task: False),
            ('snapshot_turn_input', lambda task: None),
            ('log_task_open', lambda task, tid: time.time()),
            ('make_vu_phase', lambda task: None),
            ('_emit_startup_phase', lambda *args, **kwargs: None),
            ('run_turn_prelude', lambda task, cfg, tid: cfg),
            ('bind_provider_and_affinity', lambda *args, **kwargs: None),
            ('finalize_after_loop', lambda task, **kwargs: None)):
        monkeypatch.setattr(run_mod, name, value)
    monkeypatch.setattr(run_mod, 'resolve_and_seed_model_config',
                        lambda cfg, task: {
                            'model': 'm', 'thinking_enabled': False,
                            'thinking_depth': None, 'preset': None,
                            'max_tokens': 64, 'temperature': 0,
                            'search_mode': 'off', 'search_enabled': False,
                            'fetch_enabled': False, 'project_path': '/p',
                            'project_enabled': True,
                            'code_exec_enabled': False,
                            'memory_enabled': True})

    def teardown(task, *, tid):
        clear_log_context()
        set_req_id('')

    monkeypatch.setattr(run_mod, 'finalize_task_lane', teardown)
    listing = _RecordingListing()
    task = {'id': 'feedbeef-0000-0000-0000-000000000000',
            'convId': 'conv-stop', '_userId': 1,
            'config': {'model': 'm'}, 'messages': _messages(), 'content': '',
            'aborted': True, '_abort_timestamp': time.time(),
            '_earlyMemoryListing': listing}

    run_mod.run_task(task)

    assert listing.taken == []
    assert listing.released == 1
    assert '_earlyMemoryListing' not in task


def test_terminal_task_releases_an_unconsumed_listing():
    from lib.tasks_pkg.manager._persist import _release_heavy_task_state

    listing = _RecordingListing()
    task = {'id': 'terminal', 'status': 'aborted',
            '_earlyMemoryListing': listing}

    assert _release_heavy_task_state(task) >= 1
    assert task['_earlyMemoryListing'] is None
    assert listing.released == 1


def test_release_cancels_a_listing_that_has_not_started():
    from lib.memory.prefetch import PrefetchCorpusListing

    pending = Future()
    listing = PrefetchCorpusListing('/proj', [], submit=lambda fn: pending)
    listing.release()

    assert pending.cancelled()
    assert listing.take('/proj', []) is None
