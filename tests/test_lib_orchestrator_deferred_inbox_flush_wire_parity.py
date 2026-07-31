"""Wire-parity guards for pt_03f4cdf1 slice 12 — extract post-LLM
DEFERRED peer + steer inbox flush from _run.py's stream loop into
lib.tasks_pkg.orchestrator._deferred_inbox_flush.flush_deferred_peer_and_steer().

The two flushes run RIGHT AFTER _llm_call_with_fallback returns and
BEFORE the stream loop reads the resolved model onto the task. They:

    * Pop task['_peer_inject_pending'] / task['_steer_inject_pending'],
    * Emit PEER_INBOX_INJECT / USER_STEER_INJECT chips (with correct
      previews shape),
    * Accumulate DISPLAY-ONLY sidecars task['_peerInjects'] /
      task['_userSteerInjects'] (the sync layer persists them as
      msg-underscore fields — NEVER into toolRounds),
    * De-dup the durable message_queue rows for the peer flush (VU
      sub-tasks carry the parent conv on ``_peer_drain_key``).

These tests pin the module surface and the wire-parity guards; the
actual behavioural tests already live in the orchestrator suite (the
never-zero-delivery / never-double-delivery invariants). The
delegation guards below ensure the extraction is byte-identical from
the run_task caller's viewpoint.

Failing-first: written BEFORE the extraction; each guard turns RED
until the extraction actually lands and _run.py delegates.
"""

from __future__ import annotations

import importlib
import pathlib


ROOT = pathlib.Path(__file__).resolve().parents[1]
RUN_PY = ROOT / 'lib' / 'tasks_pkg' / 'orchestrator' / '_run.py'
LEAF_PY = ROOT / 'lib' / 'tasks_pkg' / 'orchestrator' / '_deferred_inbox_flush.py'


# ---------------------------------------------------------------------------
# 1. leaf module exists and exposes the helper by name
# ---------------------------------------------------------------------------
def test_leaf_module_exists_and_exposes_flush_helper():
    """The new leaf ships a single top-level callable named
    ``flush_deferred_peer_and_steer`` — the seam name run_task will
    delegate to. Deleting the leaf or renaming the callable must break
    a downstream import."""
    mod = importlib.import_module(
        'lib.tasks_pkg.orchestrator._deferred_inbox_flush')
    assert hasattr(mod, 'flush_deferred_peer_and_steer'), (
        'lib.tasks_pkg.orchestrator._deferred_inbox_flush must export '
        'flush_deferred_peer_and_steer')
    assert callable(mod.flush_deferred_peer_and_steer)


# ---------------------------------------------------------------------------
# 2. helper signature (kw-only round_num + tid; positional task)
# ---------------------------------------------------------------------------
def test_flush_helper_signature_is_keyword_only():
    """The helper takes ``task`` positional and ``round_num`` + ``tid``
    keyword-only. Any drift breaks _run.py's call site and this test."""
    import inspect
    from lib.tasks_pkg.orchestrator._deferred_inbox_flush import (
        flush_deferred_peer_and_steer)
    sig = inspect.signature(flush_deferred_peer_and_steer)
    params = sig.parameters
    assert 'task' in params, 'task must be a parameter'
    assert 'round_num' in params, 'round_num must be a parameter'
    assert 'tid' in params, 'tid must be a parameter'
    # kw-only for the two scalars so callers cannot get argument order wrong
    assert params['round_num'].kind == inspect.Parameter.KEYWORD_ONLY
    assert params['tid'].kind == inspect.Parameter.KEYWORD_ONLY


# ---------------------------------------------------------------------------
# 3. _run.py imports and delegates to the extracted helper
# ---------------------------------------------------------------------------
def test_run_py_imports_flush_helper():
    """_run.py imports flush_deferred_peer_and_steer at module scope."""
    src = RUN_PY.read_text()
    assert 'from lib.tasks_pkg.orchestrator._deferred_inbox_flush import' in src, (
        '_run.py must import the extracted flush helper — expected an '
        '`from lib.tasks_pkg.orchestrator._deferred_inbox_flush import ...` '
        'line at module scope')
    assert 'flush_deferred_peer_and_steer' in src, (
        '_run.py must reference flush_deferred_peer_and_steer (either in the '
        'import or in the call site)')


def test_run_task_delegates_to_flush_helper():
    """The stream loop's post-LLM peer+steer flush must be a single
    call to ``flush_deferred_peer_and_steer(task, round_num=..., tid=...)``
    — no inline body left behind."""
    src = RUN_PY.read_text()
    assert 'flush_deferred_peer_and_steer(' in src, (
        '_run.py must call flush_deferred_peer_and_steer in the stream loop')


# ---------------------------------------------------------------------------
# 4. inline bodies are gone from _run.py (extraction really happened)
# ---------------------------------------------------------------------------
def test_run_py_no_longer_carries_peer_flush_inline_body():
    """The .pop('_peer_inject_pending', ...) line that started the
    inline peer flush must have moved out of _run.py."""
    src = RUN_PY.read_text()
    assert "task.pop('_peer_inject_pending'" not in src, (
        "task.pop('_peer_inject_pending', None) must live in "
        "_deferred_inbox_flush.py, not _run.py")


def test_run_py_no_longer_carries_steer_flush_inline_body():
    """The .pop('_steer_inject_pending', ...) line that started the
    inline steer flush must have moved out of _run.py."""
    src = RUN_PY.read_text()
    assert "task.pop('_steer_inject_pending'" not in src, (
        "task.pop('_steer_inject_pending', None) must live in "
        "_deferred_inbox_flush.py, not _run.py")


def test_run_py_no_longer_carries_peer_sidecar_accumulation():
    """The task['_peerInjects'] setdefault().append pattern used to live
    inside the inline peer flush — after extraction it must be inside
    the leaf, not _run.py."""
    src = RUN_PY.read_text()
    assert "'_peerInjects'" not in src, (
        "task.setdefault('_peerInjects', []) must live in the extracted "
        "leaf module, not _run.py")


def test_run_py_no_longer_carries_steer_sidecar_accumulation():
    """The task['_userSteerInjects'] setdefault().append pattern lived
    inside the inline steer flush — after extraction the string literal
    must be gone from _run.py."""
    src = RUN_PY.read_text()
    assert "'_userSteerInjects'" not in src, (
        "task.setdefault('_userSteerInjects', []) must live in the extracted "
        "leaf module, not _run.py")


# ---------------------------------------------------------------------------
# 5. leaf carries the pivotal event names + dedup call
# ---------------------------------------------------------------------------
def test_leaf_carries_peer_and_steer_event_names():
    """The leaf must reference both event names it emits — a stealth
    rename to a different EventType would break the frontend chip render
    and this guard catches it in-source."""
    src = LEAF_PY.read_text()
    assert 'PEER_INBOX_INJECT' in src, (
        'flush leaf must reference EventType.PEER_INBOX_INJECT')
    assert 'USER_STEER_INJECT' in src, (
        'flush leaf must reference EventType.USER_STEER_INJECT')


def test_leaf_carries_durable_dedup_call():
    """The leaf must call dedup_peer_durable_rows for the peer flush —
    that call is the never-double-delivery invariant enforcement."""
    src = LEAF_PY.read_text()
    assert 'dedup_peer_durable_rows' in src, (
        'flush leaf must call dedup_peer_durable_rows to enforce the '
        'never-double-delivery invariant')


def test_leaf_carries_peer_drain_key_fallback():
    """VU sub-tasks run with convId='' and carry the parent conv on
    ``_peer_drain_key`` — the leaf's peer flush MUST prefer that key
    over ``convId`` for the dedup, or a VU-issued peer message rerolls
    the durable row as a fresh turn."""
    src = LEAF_PY.read_text()
    assert '_peer_drain_key' in src, (
        'flush leaf must prefer task["_peer_drain_key"] over convId when '
        'de-duping the durable message_queue row (VU sub-task carrier)')
