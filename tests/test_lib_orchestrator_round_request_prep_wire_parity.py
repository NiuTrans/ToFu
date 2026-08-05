# Incident anchor: born in commit 25134920 — refactor(orchestrator): pt_03f4cdf1 slice 28 — extract round-request ...
# (funeral audit pt_c565a36b3e8f42e6, docs/RATCHET_AUDIT.md)
"""Wire-parity guards for pt_03f4cdf1 slice 28 — extract the
round-request preamble cluster from _run.py's stream loop into
lib.tasks_pkg.orchestrator._round_request_prep.build_round_request().

The cluster runs once per stream round after inbox drain and before
the streaming-tool accumulator construction:

    1. Gate the tool list for this round
       (``tool_list if (tool_list and round_num < max_tool_rounds) else None``),
    2. Cache-aware tool-result ordering: sort consecutive tool results
       by tool_call_id so the prefix is deterministic across rounds
       (automatic prefix caching on OpenAI/Qwen),
    3. Emit the messages-snapshot debug event (AFTER the sort so the
       panel reflects the real outbound ordering),
    4. Build the request body via the LATE-BOUND facade
       (``_o.build_body`` — a test/consumer that reassigns
       ``orchestrator.build_body`` MUST steer this call),
    5. Attach ``body['_task_id']`` for the session-stable TTL latch in
       add_cache_breakpoints (prevents mid-session cache key shift).

It returns ``(_tools_this_round, body)`` — the tool list is still
needed downstream by the round-checkpoint call (slice 20).

Failing-first: written BEFORE the extraction; the module/signature/
delegation guards turn RED until the leaf exists and _run.py
delegates.
"""

from __future__ import annotations

import importlib
import pathlib


ROOT = pathlib.Path(__file__).resolve().parents[1]
RUN_PY = ROOT / 'lib' / 'tasks_pkg' / 'orchestrator' / '_run.py'
LEAF_PY = ROOT / 'lib' / 'tasks_pkg' / 'orchestrator' / '_round_request_prep.py'


# ---------------------------------------------------------------------------
# 1. leaf module exists and exposes the helper by name
# ---------------------------------------------------------------------------
def test_leaf_module_exists_and_exposes_prep_helper():
    """The new leaf ships a single top-level callable named
    ``build_round_request`` — the seam name run_task will delegate to.
    Deleting the leaf or renaming the callable must break a downstream
    import."""
    mod = importlib.import_module(
        'lib.tasks_pkg.orchestrator._round_request_prep')
    assert hasattr(mod, 'build_round_request'), (
        'lib.tasks_pkg.orchestrator._round_request_prep must export '
        'build_round_request')
    assert callable(mod.build_round_request)


# ---------------------------------------------------------------------------
# 2. helper signature (positional carriers + kw-only scalars)
# ---------------------------------------------------------------------------
def test_prep_helper_signature():
    """The helper takes ``task, rs, messages, tool_list`` positional and
    the scalars keyword-only. Any drift breaks _run.py's call site and
    this test."""
    import inspect
    from lib.tasks_pkg.orchestrator._round_request_prep import (
        build_round_request)
    sig = inspect.signature(build_round_request)
    params = sig.parameters
    for name in ('task', 'rs', 'messages', 'tool_list'):
        assert name in params, f'{name} must be a parameter'
    for name in ('round_num', 'tid', 'max_tool_rounds', 'thinking_depth',
                 'temperature', 'max_tokens', 'response_format'):
        assert name in params, f'{name} must be a parameter'
        assert params[name].kind == inspect.Parameter.KEYWORD_ONLY, (
            f'{name} must be keyword-only')


# ---------------------------------------------------------------------------
# 3. _run.py imports and delegates to the extracted helper
# ---------------------------------------------------------------------------
def test_run_py_imports_prep_helper():
    """_run.py imports build_round_request at module scope."""
    src = RUN_PY.read_text()
    assert 'from lib.tasks_pkg.orchestrator._round_request_prep import' in src, (
        '_run.py must import the extracted prep helper — expected a '
        '`from lib.tasks_pkg.orchestrator._round_request_prep import ...` '
        'line at module scope')
    assert 'build_round_request' in src, (
        '_run.py must reference build_round_request (either in the import '
        'or in the call site)')


def test_run_task_delegates_to_prep_helper():
    """The stream loop's preamble must unpack the 2-tuple from a single
    call to ``build_round_request(...)`` — no inline body left behind."""
    src = RUN_PY.read_text()
    assert '_tools_this_round, body = build_round_request(' in src, (
        '_run.py must unpack `_tools_this_round, body = '
        'build_round_request(...)` in the stream loop')


# ---------------------------------------------------------------------------
# 4. inline bodies are gone from _run.py (extraction really happened)
# ---------------------------------------------------------------------------
def test_run_py_no_longer_sorts_tool_results_inline():
    src = RUN_PY.read_text()
    assert 'sort_tool_results(messages, conv_id=' not in src, (
        'sort_tool_results(messages, conv_id=...) must live in '
        '_round_request_prep.py, not _run.py')


def test_run_py_no_longer_builds_body_inline():
    """The late-bound facade build_body call must have moved to the
    leaf. (The string `build_body` legitimately survives in _run.py
    comments / the facade re-export note, so the guard keys on the
    `_o.build_body(` call shape.)"""
    src = RUN_PY.read_text()
    assert '_o.build_body(' not in src, (
        '_o.build_body(...) must live in _round_request_prep.py, not _run.py')


def test_run_py_no_longer_attaches_task_id_inline():
    src = RUN_PY.read_text()
    assert "body['_task_id'] = task['id']" not in src, (
        "the body['_task_id'] = task['id'] assignment must live in "
        '_round_request_prep.py, not _run.py (the bare mention in the '
        'slice-28 pointer comment is fine)')


def test_run_py_no_longer_gates_tools_inline():
    src = RUN_PY.read_text()
    assert 'tool_list if (tool_list and round_num < max_tool_rounds)' not in src, (
        'the _tools_this_round gating expression must live in '
        '_round_request_prep.py, not _run.py')


# ---------------------------------------------------------------------------
# 5. leaf carries the pivotal semantics (order + late binding + attach)
# ---------------------------------------------------------------------------
def test_leaf_preserves_step_ordering():
    """sort_tool_results MUST run before the snapshot emission, which
    MUST run before build_body — the snapshot reflects the real
    outbound ordering, and the body is built from the sorted messages."""
    src = LEAF_PY.read_text()
    i_sort = src.index('sort_tool_results(')
    i_snap = src.index('emit_messages_snapshot_event(')
    i_body = src.index('_o.build_body(')
    assert i_sort < i_snap < i_body, (
        'leaf must order sort_tool_results → emit_messages_snapshot_event '
        '→ _o.build_body (snapshot sees the sorted wire ordering; body is '
        'built from the sorted messages)')


def test_leaf_builds_body_via_late_bound_facade():
    """The leaf MUST call ``_o.build_body`` (module-attribute access on
    the orchestrator facade at CALL time) — never a from-import binding
    — so a test/consumer reassigning ``orchestrator.build_body`` steers
    this call. This invariant is documented at _run.py's own import of
    the facade."""
    src = LEAF_PY.read_text()
    assert 'import lib.tasks_pkg.orchestrator as _o' in src, (
        'leaf must bind the facade module (import lib.tasks_pkg.orchestrator '
        'as _o) for late-bound build_body access')
    assert '_o.build_body(' in src, (
        'leaf must call _o.build_body(...) — late binding preserved')


def test_leaf_attaches_task_id_for_cache_ttl():
    """The leaf MUST attach body['_task_id'] — the session-stable TTL
    latch in add_cache_breakpoints prevents mid-session cache key
    shift."""
    src = LEAF_PY.read_text()
    assert "body['_task_id'] = task['id']" in src, (
        "leaf must attach body['_task_id'] = task['id'] (cache-TTL latch)")


def test_leaf_carries_tools_gating_expression():
    """The tool list is gated to None once round_num >= max_tool_rounds
    — the model must see an empty tool surface on the forced-final
    round."""
    src = LEAF_PY.read_text()
    assert 'round_num < max_tool_rounds' in src, (
        'leaf must gate the tool list on round_num < max_tool_rounds')


def test_leaf_returns_two_tuple():
    """The leaf returns (_tools_this_round, body) — the tool list is
    still needed downstream by the round-checkpoint call."""
    src = LEAF_PY.read_text()
    assert 'return _tools_this_round, body' in src, (
        'leaf must `return _tools_this_round, body`')
