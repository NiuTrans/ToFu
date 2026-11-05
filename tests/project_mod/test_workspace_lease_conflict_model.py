"""Executable conflict model for a checkout whose linked worktrees nest inside it.

Authority: docs/runbooks/GIT_WORKSPACE_GUARD.md, "Conflict model and admission
order". The layout is the swarm layout of the 2026-10-28 stall: linked
worktrees A and B live under ROOT/.tofu/worktrees. A long internal operation
inside A holds its claims for the whole test; each probe asks for one
operation's claims with ``timeout=0`` and records whether it was admitted at
once. Model tool calls take no lease at all ("Tool calls take no workspace
leases"); these claims are those of Tofu's own whole-workspace operations.

Every lease key lives in a private namespace, never the server's.
"""

import contextlib
import subprocess
import threading
import time
from types import SimpleNamespace

import pytest

from lib.project_mod import workspace_lease as leases
from lib.project_mod.tool_admission import Cancellation, tool_admission_scope

pytestmark = pytest.mark.unit


def _git(*args):
    subprocess.run(['git', *args], check=True, capture_output=True)


@pytest.fixture
def layout(tmp_path, monkeypatch):
    namespace = tmp_path / 'leases'
    namespace.mkdir(mode=0o700)
    monkeypatch.setattr(leases, '_tree_lock_namespace', lambda: namespace)
    root = (tmp_path / 'root').resolve()
    root.mkdir()
    _git('init', '-q', str(root))
    _git('-C', str(root), 'config', 'user.email', 'model@example.invalid')
    _git('-C', str(root), 'config', 'user.name', 'Model')
    (root / '.gitignore').write_text('.tofu/\n')
    (root / 'file.txt').write_text('x\n')
    _git('-C', str(root), 'add', '.')
    _git('-C', str(root), 'commit', '-qm', 'init')
    _git('-C', str(root), 'branch', 'feature')
    worktrees = root / '.tofu' / 'worktrees'
    for name in ('A', 'B'):
        _git('-C', str(root), 'worktree', 'add', '-q', str(worktrees / name), '-b', 'w' + name)
    return SimpleNamespace(root=str(root), a=str(worktrees / 'A'), b=str(worktrees / 'B'),
                           new=str(worktrees / 'new'), namespace=namespace)


@contextlib.contextmanager
def _held(claims, *, task_id='holder-task', purpose=''):
    """Hold *claims* on another thread, as a long internal operation would."""
    entered, release, errors = threading.Event(), threading.Event(), []
    task = {'id': task_id, 'convId': 'holder-conv', 'aborted': False}

    def hold():
        try:
            with tool_admission_scope(task, tool_name='integration'), leases.workspace_claims_lease(
                    claims, timeout=5.0, cancel_event=Cancellation(task), purpose=purpose):
                entered.set()
                release.wait(30)
        except BaseException as error:  # pragma: no cover - surfaces below
            errors.append(error)
            entered.set()

    thread = threading.Thread(target=hold, daemon=True)
    thread.start()
    assert entered.wait(10) and not errors, errors
    try:
        yield
    finally:
        release.set()
        thread.join(10)
    assert not errors, errors


def _admitted_at_once(claims):
    try:
        with leases.workspace_claims_lease(claims, timeout=0.0):
            return True
    except leases.WorkspaceBusyError:
        return False


# A long internal operation inside nested worktree A, by the claim kind it holds.
_HOLDERS = {
    'tree:A': lambda L: [('tree', L.a)],
    'file:A/out.log': lambda L: [('file', L.a + '/out.log')],
    'root:A': lambda L: [('root', L.a)],
    'metadata:A': lambda L: [('metadata', L.a)],
}
_HOLDER_ORDER = tuple(_HOLDERS)

# Operation -> (claims, admitted at once per holder in _HOLDER_ORDER).
_T, _F = True, False
_OPERATIONS = {
    'root git merge --ff-only': (lambda L: [('root', L.root)], (_T, _T, _T, _T)),
    # One Git common directory: refs/objects/worktree administration are shared
    # by every worktree, so a metadata holder in A truly excludes them.
    'root git commit / branch -d': (lambda L: [('metadata', L.root)], (_T, _T, _T, _F)),
    'root git worktree add (new)': (
        lambda L: [('tree', L.new), ('metadata', L.root)], (_T, _T, _T, _F)),
    'root git worktree remove B': (
        lambda L: [('tree', L.b), ('metadata', L.root)], (_T, _T, _T, _F)),
    'edit_file in B': (lambda L: [('file', L.b + '/x.txt')], (_T, _T, _T, _T)),
    'read-only command (no claims)': (lambda L: [], (_T, _T, _T, _T)),
    # Truly mutates A's files: must wait for every holder inside A.
    'root git worktree remove A': (
        lambda L: [('tree', L.a), ('metadata', L.root)], (_F, _F, _F, _F)),
    # An exclusive tree claim on ROOT is a destructive-ancestor fence over
    # every nested worktree (the 2026-10-28 waiter's misclassified git log).
    'tree claim on ROOT': (lambda L: [('tree', L.root)], (_F, _F, _F, _F)),
    'edit_file in A': (lambda L: [('file', L.a + '/x.txt')], (_F, _T, _F, _T)),
}


@pytest.mark.parametrize('operation', tuple(_OPERATIONS))
def test_conflict_matrix_against_long_holders_in_nested_worktree(layout, operation):
    claims_of, expected = _OPERATIONS[operation]
    observed = []
    for name in _HOLDER_ORDER:
        with _held(_HOLDERS[name](layout)):
            observed.append(_admitted_at_once(claims_of(layout)))
    assert dict(zip(_HOLDER_ORDER, observed)) == dict(zip(_HOLDER_ORDER, expected))


def test_incident_replay_tree_claim_on_root_waits_for_the_nested_holder(layout):
    """2026-10-28: git-uncatalogued tree+metadata on ROOT behind tree:A."""
    with _held([('tree', layout.a)]):
        assert not _admitted_at_once([('tree', layout.root), ('metadata', layout.root)])
    assert _admitted_at_once([('tree', layout.root), ('metadata', layout.root)])


# ── Admission order: an exclusive waiter is not starved by shared holders ──

def _shared_relay(layout, stop, hold_seconds=0.2, overlap_after=0.02):
    """Overlapping shared holders below ROOT: each enters before the last leaves."""
    threads = []

    def holder(index, entered):
        claims = [('file', f'{layout.b}/relay-{index}.txt')]
        with leases.workspace_claims_lease(claims, timeout=None):
            entered.set()
            time.sleep(hold_seconds)

    def control():
        index = 0
        while not stop.is_set():
            entered = threading.Event()
            thread = threading.Thread(target=holder, args=(index, entered), daemon=True)
            threads.append(thread)
            thread.start()
            while not entered.wait(0.05):
                if stop.is_set():
                    return
            time.sleep(overlap_after)
            index += 1

    controller = threading.Thread(target=control, daemon=True)
    controller.start()
    return controller, threads


def test_exclusive_waiter_is_admitted_through_overlapping_shared_holders(layout):
    stop = threading.Event()
    controller, threads = _shared_relay(layout, stop)
    try:
        time.sleep(0.3)  # the relay is continuous before the writer arrives
        started = time.monotonic()
        with leases.workspace_claims_lease([('tree', layout.root)], timeout=3.0):
            waited = time.monotonic() - started
    finally:
        stop.set()
        controller.join(10)
        for thread in threads:
            thread.join(10)
    # Admitted once the holders it found leave (each holds 0.2 s).
    assert waited < 1.5


def _queued_writer(layout, task):
    """An exclusive tree:ROOT request waiting on another thread."""
    outcome = []

    def wait():
        try:
            with leases.workspace_claims_lease([('tree', layout.root)], timeout=None,
                                               cancel_event=Cancellation(task)):
                outcome.append(('admitted', time.monotonic()))
        except leases.WorkspaceLeaseCancelled:
            outcome.append(('cancelled', time.monotonic()))

    thread = threading.Thread(target=wait, daemon=True)
    thread.start()
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and not _queued_on_root(layout):
        time.sleep(0.01)
    assert _queued_on_root(layout), 'the exclusive request never queued'
    return thread, outcome


def _queued_on_root(layout):
    key = 'tree:' + layout.root
    with leases._registry_lock:
        entries = [entry for (root, _common), entry in leases._registry.items() if root == key]
    return any(entry.get('queued') for entry in entries)


def test_compatible_newcomer_waits_behind_a_queued_writer_only_while_holders_are_young(
        layout, monkeypatch):
    monkeypatch.setattr(leases, '_QUEUE_ORDER_MAX_HOLDER_AGE_SECONDS', 0.6)
    writer_task = {'id': 'writer-task', 'convId': 'writer-conv', 'aborted': False}
    with _held([('file', layout.a + '/out.log')]):
        writer, outcome = _queued_writer(layout, writer_task)
        newcomer = [('file', layout.b + '/x.txt')]
        # Overtaking the queued writer is refused while the holder it waits
        # for is young; the refusal names the queued request.
        with pytest.raises(leases.WorkspaceBusyError, match='queued'):
            with leases.workspace_claims_lease(newcomer, timeout=0.0):
                pass
        # Bounded: once that holder has held for the bound, the writer is
        # behind a long-running holder and the newcomer is not frozen.
        started = time.monotonic()
        with leases.workspace_claims_lease(newcomer, timeout=3.0):
            waited = time.monotonic() - started
        assert waited < 1.5
        assert outcome == []  # the writer still waits for the long holder
    writer.join(10)
    assert [state for state, _ in outcome] == ['admitted']


def test_long_running_holder_does_not_freeze_newcomers_behind_a_queued_writer(
        layout, monkeypatch):
    monkeypatch.setattr(leases, '_QUEUE_ORDER_MAX_HOLDER_AGE_SECONDS', 0.3)
    writer_task = {'id': 'writer-task', 'convId': 'writer-conv', 'aborted': False}
    with _held([('file', layout.a + '/out.log')]):
        time.sleep(0.5)  # the holder is already long-running
        writer, outcome = _queued_writer(layout, writer_task)
        assert _admitted_at_once([('file', layout.b + '/x.txt')])
        assert outcome == []
    writer.join(10)
    assert [state for state, _ in outcome] == ['admitted']


def test_reentrant_holder_is_never_held_back_by_queue_order(layout):
    writer_task = {'id': 'writer-task', 'convId': 'writer-conv', 'aborted': False}
    with leases.workspace_claims_lease([('file', layout.b + '/x.txt')], timeout=0.0):
        writer, outcome = _queued_writer(layout, writer_task)
        # Same thread, keys it already holds (tree:ROOT barrier and ancestors).
        assert _admitted_at_once([('file', layout.b + '/y.txt')])
        assert outcome == []
    writer.join(10)
    assert [state for state, _ in outcome] == ['admitted']


def test_cancelled_queued_writer_stops_holding_newcomers_back(layout):
    writer_task = {'id': 'writer-task', 'convId': 'writer-conv', 'aborted': False}
    with _held([('file', layout.a + '/out.log')]):
        writer, outcome = _queued_writer(layout, writer_task)
        assert not _admitted_at_once([('file', layout.b + '/x.txt')])
        writer_task['aborted'] = True
        writer.join(10)
        assert [state for state, _ in outcome] == ['cancelled']
        assert _admitted_at_once([('file', layout.b + '/x.txt')])
    assert not leases._registry
