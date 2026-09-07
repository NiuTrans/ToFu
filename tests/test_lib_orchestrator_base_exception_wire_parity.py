"""Wire-parity guards for pt_03f4cdf1 slice 34 — extract the BaseException
fatal handler from _run.py into
lib.tasks_pkg.orchestrator._post_loop.handle_task_base_exception().

The non-Exception fatal path (KeyboardInterrupt / SystemExit /
asyncio.CancelledError derive from BaseException, NOT Exception, so they
slip past the ordinary handler) must still emit the terminal DONE(error)
so the terminal-callback chain (admission-slot release + billing settle
via on_terminal) fires — otherwise the task stays NON-TERMINAL forever,
stranding its slot AND its billing reservation until the janitor reclaims
them. Then RE-RAISE to preserve the cancel/shutdown semantics for the
caller.

Contract preserved byte-for-byte:
  * ERROR log with exc_info first,
  * task['error'] = internal envelope ('Task terminated: <Type>'),
    status/finishReason = 'error',
  * DONE event + persist_task_result ONLY when NOT _flow_managed,
  * every step of the finalize wrapped so a finalize failure logs but
    never masks the original BaseException,
  * always re-raises the ORIGINAL object.

Failing-first: written BEFORE the extraction; the module/signature/
delegation guards turn RED until the leaf exists and _run.py delegates.
"""

from __future__ import annotations

import importlib
import pathlib


ROOT = pathlib.Path(__file__).resolve().parents[1]
RUN_PY = ROOT / 'lib' / 'tasks_pkg' / 'orchestrator' / '_run.py'
LEAF_PY = ROOT / 'lib' / 'tasks_pkg' / 'orchestrator' / '_post_loop.py'


# ---------------------------------------------------------------------------
# 1. leaf exposes the helper
# ---------------------------------------------------------------------------
def test_post_loop_exposes_base_exception_handler():
    mod = importlib.import_module('lib.tasks_pkg.orchestrator._post_loop')
    assert hasattr(mod, 'handle_task_base_exception') \
        and callable(mod.handle_task_base_exception), (
            '_post_loop must export handle_task_base_exception')


def test_handler_signature():
    import inspect
    from lib.tasks_pkg.orchestrator._post_loop import (
        handle_task_base_exception)
    params = inspect.signature(handle_task_base_exception).parameters
    assert 'task' in params and 'be' in params, (
        'handle_task_base_exception takes (task, be)')


# ---------------------------------------------------------------------------
# 2. _run.py delegates
# ---------------------------------------------------------------------------
def test_run_py_calls_handler_in_base_exception_branch():
    src = RUN_PY.read_text()
    assert 'handle_task_base_exception(task, be)' in src, (
        '_run.py must delegate the BaseException branch')


def test_run_py_leaf_reraises_not_caller():
    """The re-raise lives INSIDE the leaf (raise be); the caller branch
    is a bare delegation. The envelope / DONE / persist body must be
    gone from _run.py."""
    src = RUN_PY.read_text()
    assert 'task-fatal-base' not in src, (
        "the 'task-fatal-base' envelope must live in _post_loop.py")
    import re
    assert not re.search(r'except BaseException as be:[\s\S]{0,400}?task\[.error.\]', src), (
        'the BaseException branch must not stamp the envelope inline')


# ---------------------------------------------------------------------------
# 3. leaf carries the pivotal semantics
# ---------------------------------------------------------------------------
def test_leaf_carries_envelope_done_persist_reraise_contract():
    src = LEAF_PY.read_text()
    for needle in ('task-fatal-base', "'internal'",
                   "task['status'] = 'error'",
                   "finishReason",
                   'persist_task_result',
                   '_flow_managed',
                   'exc_info=True'):
        assert needle in src, f'leaf missing {needle}'


# ---------------------------------------------------------------------------
# 4. BEHAVIOURAL: full terminal-persist path + Flow-managed skip +
#    finalize-failure fail-open (owner directive)
# ---------------------------------------------------------------------------
def _drive(leaf, monkeypatch, task, be):
    monkeypatch.setattr(leaf, 'persist_task_result',
                        lambda t: leaf._calls.append(('persist', t['status'])))
    monkeypatch.setattr(leaf, 'append_event',
                        lambda t, ev: leaf._calls.append(('done', ev)))
    monkeypatch.setattr(leaf, 'build_event',
                        lambda et, **kw: ('EV', et, kw))
    try:
        leaf.handle_task_base_exception(task, be)
    except BaseException as r:
        return r
    raise AssertionError('must re-raise the original BaseException')


def test_behaviour_terminal_done_persist_and_reraise(monkeypatch):
    import lib.tasks_pkg.orchestrator._post_loop as leaf
    leaf._calls = []
    task = {'id': 'deadbeefcafe', 'config': {'model': 'm'}}
    raised = _drive(leaf, monkeypatch, task, KeyboardInterrupt())
    assert isinstance(raised, KeyboardInterrupt), (
        'the ORIGINAL BaseException must re-raise')
    assert task['status'] == 'error' and task['finishReason'] == 'error'
    assert 'Task terminated: KeyboardInterrupt' in str(task['error'])
    kinds = [c[0] for c in leaf._calls]
    assert 'done' in kinds and 'persist' in kinds, (
        'ordinary task must emit DONE + persist')


def test_behaviour_flow_managed_skips_done_persist(monkeypatch):
    import lib.tasks_pkg.orchestrator._post_loop as leaf
    leaf._calls = []
    task = {'id': 'deadbeefcafe', 'config': {}, '_flow_managed': True}
    raised = _drive(leaf, monkeypatch, task, SystemExit(3))
    assert isinstance(raised, SystemExit)
    assert task['status'] == 'error', 'envelope still stamped'
    assert leaf._calls == [], (
        'Flow-managed task must NOT emit DONE/persist here (the '
        'endpoint lane owns terminal emission)')


def test_behaviour_finalize_failure_never_masks_original(monkeypatch):
    """A build_event failure inside the finalize must log + still re-raise
    the ORIGINAL BaseException (fail-open)."""
    import lib.tasks_pkg.orchestrator._post_loop as leaf
    leaf._calls = []
    monkeypatch.setattr(leaf, 'persist_task_result', lambda t: None)
    monkeypatch.setattr(leaf, 'append_event',
                        lambda *a: (_ for _ in ()).throw(RuntimeError('boom')))
    monkeypatch.setattr(leaf, 'build_event',
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError('boom')))
    task = {'id': 'deadbeefcafe', 'config': {}}
    try:
        leaf.handle_task_base_exception(task, KeyboardInterrupt())
    except KeyboardInterrupt:
        return
    raise AssertionError('original must re-raise even when finalize fails')
