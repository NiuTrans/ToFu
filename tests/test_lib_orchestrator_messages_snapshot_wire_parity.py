"""Wire-parity guards for pt_03f4cdf1 slice 15 — extract the
Request-Inspector messages-snapshot emission block from _run.py's
stream loop into
lib.tasks_pkg.orchestrator._messages_snapshot
    .emit_messages_snapshot_event().

The cluster runs RIGHT AFTER ``sort_tool_results`` — messages are now
in their real outbound ordering, so the debug panel sees the same
sequence the model will. The cluster:

    * Runs ``apply_wire_sanitize`` on an INDEPENDENT copy of ``messages``
      (never mutating the live list — build_body re-runs its own copy
      at request time).
    * Strips base64 data URLs from the snapshot via
      ``_strip_base64_for_snapshot`` — keeps the debug event small
      enough to travel over SSE.
    * Builds a MESSAGES_SNAPSHOT event with kind='request' (the ONLY
      such site — post-tool / final / fallback are kind='state').
    * Appends the event to the task's event log so the Request
      Inspector can render the pre-flight snapshot.

Pure side-effect emission — no early exits, no loop-mutation, no
closure captures beyond the passed args. That is why it is a clean cut.

Failing-first: this file is written BEFORE the extraction lands. Each
guard turns RED until the extraction really happens and the
delegation call replaces the inline body in _run.py.
"""

from __future__ import annotations

import importlib
import inspect
import pathlib


ROOT = pathlib.Path(__file__).resolve().parents[1]
RUN_PY = ROOT / 'lib' / 'tasks_pkg' / 'orchestrator' / '_run.py'
LEAF_PY = (
    ROOT / 'lib' / 'tasks_pkg' / 'orchestrator' /
    '_messages_snapshot.py')
# Slice 28 (2026-07-31) moved the snapshot call site out of _run.py into
# the round-request-prep leaf: the snapshot runs as step 3 of the
# preamble cluster (sort → snapshot → build_body), and the whole
# cluster now lives in _round_request_prep.build_round_request —
# _run.py delegates the whole cluster. The two wiring guards below
# therefore assert on the prep leaf, not _run.py.
PREP_PY = (
    ROOT / 'lib' / 'tasks_pkg' / 'orchestrator' /
    '_round_request_prep.py')


# ---------------------------------------------------------------------------
# 1. leaf module exists and exposes the helper by name
# ---------------------------------------------------------------------------
def test_leaf_module_exists_and_exposes_helper():
    mod = importlib.import_module(
        'lib.tasks_pkg.orchestrator._messages_snapshot')
    assert hasattr(mod, 'emit_messages_snapshot_event'), (
        'lib.tasks_pkg.orchestrator._messages_snapshot must export '
        'emit_messages_snapshot_event')
    assert callable(mod.emit_messages_snapshot_event)


# ---------------------------------------------------------------------------
# 2. helper signature — kw-only scalars so callers can't get order wrong
# ---------------------------------------------------------------------------
def test_helper_signature_is_keyword_only():
    """The helper takes ``task`` and ``messages`` positional and every
    scalar kw-only (tid, round_num, model, thinking_enabled,
    thinking_depth, preset, temperature, max_tokens, response_format,
    tools). A signature drift breaks _run.py's call site."""
    from lib.tasks_pkg.orchestrator._messages_snapshot import (
        emit_messages_snapshot_event)
    sig = inspect.signature(emit_messages_snapshot_event)
    params = sig.parameters
    assert 'task' in params
    assert 'messages' in params
    for name in ('tid', 'round_num', 'model',
                 'thinking_enabled', 'thinking_depth', 'preset',
                 'temperature', 'max_tokens', 'response_format', 'tools'):
        assert name in params, (
            f'emit_messages_snapshot_event must accept {name}')
        assert params[name].kind == inspect.Parameter.KEYWORD_ONLY, (
            f'{name} must be keyword-only')


# ---------------------------------------------------------------------------
# 3. _run.py imports + delegates to the extracted helper
# ---------------------------------------------------------------------------
def test_run_py_imports_helper():
    """The round-request-prep leaf imports emit_messages_snapshot_event
    at module scope. (Was _run.py before slice 28 moved the preamble
    cluster.)"""
    src = PREP_PY.read_text()
    assert (
        'from lib.tasks_pkg.orchestrator._messages_snapshot import'
        in src), (
        '_round_request_prep.py must import the extracted helper — '
        'expected a `from lib.tasks_pkg.orchestrator._messages_snapshot '
        'import ...` line at module scope')
    assert 'emit_messages_snapshot_event' in src


def test_run_task_delegates_to_helper():
    """The preamble's snapshot emission must be a single call to
    ``emit_messages_snapshot_event(task, messages, ...)`` — no inline
    body left behind. (Call site moved from _run.py's run_task to
    _round_request_prep.build_round_request in slice 28.)"""
    src = PREP_PY.read_text()
    assert 'emit_messages_snapshot_event(' in src, (
        '_round_request_prep.py must call emit_messages_snapshot_event '
        'as preamble step 3 (after sort_tool_results, before build_body)')


# ---------------------------------------------------------------------------
# 4. inline bodies are gone from _run.py (extraction really happened)
# ---------------------------------------------------------------------------
def test_run_py_no_longer_carries_apply_wire_sanitize_call():
    """The inline ``apply_wire_sanitize(`` call site must have moved
    into the leaf (the IMPORT of apply_wire_sanitize stays only if
    other paths need it; if not, it should be gone too)."""
    src = RUN_PY.read_text()
    code_lines = [
        ln for ln in src.splitlines()
        if not ln.lstrip().startswith('#')
    ]
    code = '\n'.join(code_lines)
    assert 'apply_wire_sanitize(' not in code, (
        'apply_wire_sanitize(...) call site must live in '
        '_messages_snapshot.py, not _run.py')


def test_run_py_no_longer_carries_strip_base64_call():
    """The ``_strip_base64_for_snapshot(`` call site moved into the leaf."""
    src = RUN_PY.read_text()
    code_lines = [
        ln for ln in src.splitlines()
        if not ln.lstrip().startswith('#')
    ]
    code = '\n'.join(code_lines)
    assert '_strip_base64_for_snapshot(' not in code, (
        '_strip_base64_for_snapshot(...) call site must live in '
        '_messages_snapshot.py, not _run.py')


def test_run_py_no_longer_carries_snapshot_build_event():
    """The build_event(EventType.MESSAGES_SNAPSHOT, ...) call for the
    request-kind snapshot must be gone from _run.py — the OTHER
    snapshot sites (post-tool / final / fallback in _turn.py or
    _finalize.py) are kind='state' and MAY still call build_event
    with MESSAGES_SNAPSHOT there, but the kind='request' branch
    lives in this leaf now."""
    src = RUN_PY.read_text()
    code_lines = [
        ln for ln in src.splitlines()
        if not ln.lstrip().startswith('#')
    ]
    code = '\n'.join(code_lines)
    # The characteristic kind='request' MESSAGES_SNAPSHOT emission
    # signature must be gone.
    assert "EventType.MESSAGES_SNAPSHOT" not in code, (
        'The MESSAGES_SNAPSHOT emission at the pre-request site must '
        'live in _messages_snapshot.py, not _run.py')


# ---------------------------------------------------------------------------
# 5. leaf really carries the moved-out call sites
# ---------------------------------------------------------------------------
def test_leaf_carries_apply_wire_sanitize_call():
    src = LEAF_PY.read_text()
    assert 'apply_wire_sanitize(' in src, (
        'emit_messages_snapshot_event must call apply_wire_sanitize')


def test_leaf_carries_strip_base64_call():
    src = LEAF_PY.read_text()
    assert '_strip_base64_for_snapshot(' in src, (
        'emit_messages_snapshot_event must call '
        '_strip_base64_for_snapshot')


def test_leaf_carries_request_kind_marker():
    """The kind='request' contract line is load-bearing: docs/DEBUG_
    PANEL_REDESIGN.md §3 says this is the ONLY kind='request'
    emission — losing that string turns it into a kind='state' event
    and the Request Inspector can no longer distinguish pre-flight
    from post-tool / final."""
    src = LEAF_PY.read_text()
    assert "kind='request'" in src, (
        "emit_messages_snapshot_event must emit kind='request' — the "
        'ONLY pre-flight MESSAGES_SNAPSHOT site')


def test_leaf_carries_endpoint_phase_turn_tag():
    """Endpoint-mode tasks (Planner/Worker/Critic) each re-run
    run_task with their own round numbering, so the driver's phase is
    tagged onto the event via ``turn=task.get('_endpoint_phase') or
    ''``. Losing this tag makes same-numbered rounds
    indistinguishable in the Request Inspector."""
    src = LEAF_PY.read_text()
    assert "_endpoint_phase" in src, (
        'emit_messages_snapshot_event must forward _endpoint_phase as '
        'the turn tag on the emitted event')


def test_leaf_carries_independent_copy_semantics():
    """The wire-sanitized snapshot must run on an INDEPENDENT copy —
    build_body re-runs the sanitizer on its own copy at request time,
    so a shared mutation would double-sanitize."""
    src = LEAF_PY.read_text()
    # The apply_wire_sanitize call itself is the primitive that produces
    # the independent copy; the load-bearing marker is that we consume
    # the RETURN value (assigned to _wire / snapshot / similar) and
    # never mutate `messages` in-place. Assert we assign the return:
    assert (
        '_wire = apply_wire_sanitize(' in src
        or 'wire = apply_wire_sanitize(' in src
        or 'snapshot = apply_wire_sanitize(' in src), (
        'emit_messages_snapshot_event must capture the return value of '
        'apply_wire_sanitize (never in-place)')


# ---------------------------------------------------------------------------
# 6. behavioural: helper is best-effort — never raises on inner failures
# ---------------------------------------------------------------------------
def test_helper_swallows_exceptions_from_wire_sanitize(monkeypatch):
    """The inline body was try/except-wrapped so a Request Inspector
    failure never breaks the LLM round. The extracted helper must
    reproduce that contract."""
    import lib.tasks_pkg.orchestrator._messages_snapshot as mod

    class _Boom(Exception):
        pass

    def _broken_apply(*_a, **_k):
        raise _Boom('wire sanitize failed')

    monkeypatch.setattr(mod, 'apply_wire_sanitize', _broken_apply)

    # No exception must escape — the LLM round survives.
    import threading
    task = {'id': 'a' * 32, 'convId': 'conv-x', 'events': [],
            'events_lock': threading.RLock()}
    mod.emit_messages_snapshot_event(
        task, [],
        tid='abcd1234', round_num=0, model='claude-x',
        thinking_enabled=False, thinking_depth=0, preset='default',
        temperature=1.0, max_tokens=8192, response_format=None, tools=None)


def _make_task(**overrides):
    """Build a synthetic task dict with the fields append_event requires.

    ``append_event`` uses ``task['events_lock']`` for thread-safe append —
    the tests must supply one, real run_task creates it in setup."""
    import threading
    t = {
        'id': 'a' * 32, 'convId': 'conv-x', 'events': [],
        '_endpoint_phase': '', 'events_lock': threading.RLock(),
    }
    t.update(overrides)
    return t


def test_helper_emits_event_on_success():
    """Happy path: on success the helper must append exactly one
    MESSAGES_SNAPSHOT event onto ``task['events']`` with the right
    shape."""
    from lib.tasks_pkg.orchestrator._messages_snapshot import (
        emit_messages_snapshot_event)
    task = _make_task()
    messages = [
        {'role': 'user', 'content': 'hello'},
    ]
    emit_messages_snapshot_event(
        task, messages,
        tid='abcd1234', round_num=0, model='claude-x',
        thinking_enabled=False, thinking_depth=0, preset='default',
        temperature=1.0, max_tokens=8192, response_format=None, tools=None)
    # Filter for MESSAGES_SNAPSHOT events (event_type is a string on the
    # event dict, spelling is enforced by build_event).
    snapshots = [
        e for e in task['events']
        if e.get('type') == 'messages_snapshot' or e.get('event_type') == 'messages_snapshot'
        or 'messages_snapshot' in str(e).lower()
    ]
    assert snapshots, (
        f'emit_messages_snapshot_event must append a MESSAGES_SNAPSHOT '
        f'event to task events; got {task["events"][:2]}')


def test_helper_attaches_tools_only_when_present():
    """When _tools_this_round is non-empty, it must be attached to the
    emitted event under the 'tools' key; when empty/None, the 'tools'
    key must NOT be present (defensive against dashboard code that
    inspects presence)."""
    from lib.tasks_pkg.orchestrator._messages_snapshot import (
        emit_messages_snapshot_event)
    # With tools
    task_a = _make_task(id='a' * 32, convId='c1')
    emit_messages_snapshot_event(
        task_a, [{'role': 'user', 'content': 'x'}],
        tid='t1', round_num=0, model='m', thinking_enabled=False,
        thinking_depth=0, preset='default', temperature=1.0,
        max_tokens=100, response_format=None,
        tools=[{'name': 'grep'}])
    ev_a = task_a['events'][-1]
    assert ev_a.get('tools') == [{'name': 'grep'}], (
        'tools list must be attached when non-empty')

    # Without tools
    task_b = _make_task(id='b' * 32, convId='c2')
    emit_messages_snapshot_event(
        task_b, [{'role': 'user', 'content': 'x'}],
        tid='t2', round_num=0, model='m', thinking_enabled=False,
        thinking_depth=0, preset='default', temperature=1.0,
        max_tokens=100, response_format=None, tools=None)
    ev_b = task_b['events'][-1]
    assert 'tools' not in ev_b, (
        "'tools' key must be ABSENT when tools list is empty/None — "
        'presence is load-bearing for the Request Inspector')
