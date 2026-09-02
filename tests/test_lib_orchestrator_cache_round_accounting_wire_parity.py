"""Wire-parity guards for pt_03f4cdf1 slice 13 — extract per-round
prompt cache-break detection + tool-call name + write-breakdown
stamping + log_round_cache_stats logging call from _run.py's stream
loop into
lib.tasks_pkg.orchestrator._cache_round_accounting.stamp_round_cache_accounting().

The cluster runs RIGHT AFTER the post-LLM ``flush_deferred_peer_and_steer``
call (pt_03f4cdf1 slice 12) — the LLM call has returned, ``rs.last_usage``
is set, and the round's entry has been appended to ``rs.api_rounds``.
The cluster:

    * Runs ``detect_cache_break(convId, messages, tools, model, usage)`` and
      stamps its verdict onto ``rs.api_rounds[-1]['cacheBreak']`` (when the
      last recorded round matches this round).
    * Extracts the tool_call function names from ``rs.assistant_msg`` and
      stamps them onto ``rs.api_rounds[-1]['toolCalls']`` for the next
      round's write breakdown.
    * Computes the round's write breakdown via ``_compute_write_breakdown``
      (with a ``prev_turn_cache_read`` baseline on round-1 turns) and
      stamps it onto ``rs.api_rounds[-1]['writeBreakdown']``.
    * Calls ``log_round_cache_stats`` at INFO level for production
      cache-usage visibility.

The cluster is fully guarded by ``if task.get('convId') and rs.last_usage:``
— non-conv turns and turns with no usage are silent no-ops.

Failing-first: this test file is written BEFORE the extraction lands.
Each guard turns RED until the extraction really happens and the
delegation call replaces the inline body in _run.py.
"""

from __future__ import annotations

import importlib
import inspect
import pathlib

import pytest


pytestmark = pytest.mark.unit


ROOT = pathlib.Path(__file__).resolve().parents[1]
RUN_PY = ROOT / 'lib' / 'tasks_pkg' / 'orchestrator' / '_run.py'
ROOT_LOOP_PY = (
    ROOT / 'lib' / 'tasks_pkg' / 'orchestrator' / '_root_agent_loop.py')
LEAF_PY = (
    ROOT / 'lib' / 'tasks_pkg' / 'orchestrator' /
    '_cache_round_accounting.py')


# ---------------------------------------------------------------------------
# 1. leaf module exists and exposes the helper by name
# ---------------------------------------------------------------------------
def test_leaf_module_exists_and_exposes_helper():
    """The new leaf ships a single top-level callable named
    ``stamp_round_cache_accounting`` — the seam name run_task
    delegates to. Deleting the leaf or renaming the callable must
    break the downstream import."""
    mod = importlib.import_module(
        'lib.tasks_pkg.orchestrator._cache_round_accounting')
    assert hasattr(mod, 'stamp_round_cache_accounting'), (
        'lib.tasks_pkg.orchestrator._cache_round_accounting must export '
        'stamp_round_cache_accounting')
    assert callable(mod.stamp_round_cache_accounting)


# ---------------------------------------------------------------------------
# 2. helper signature — kw-only scalars so callers can't get order wrong
# ---------------------------------------------------------------------------
def test_helper_signature_is_keyword_only():
    """The helper takes ``task`` positional and the round-scoped
    scalars kw-only (round_num, tid, model, tools, usage,
    assistant_msg, api_rounds, messages). A signature drift breaks
    _run.py's call site and this test bites."""
    from lib.tasks_pkg.orchestrator._cache_round_accounting import (
        stamp_round_cache_accounting)
    sig = inspect.signature(stamp_round_cache_accounting)
    params = sig.parameters
    assert 'task' in params
    for name in ('round_num', 'tid', 'model', 'tools', 'usage',
                 'assistant_msg', 'api_rounds', 'messages'):
        assert name in params, (
            f'stamp_round_cache_accounting must accept {name}')
        assert params[name].kind == inspect.Parameter.KEYWORD_ONLY, (
            f'{name} must be keyword-only')


# ---------------------------------------------------------------------------
# 3. _run.py imports and delegates to the extracted helper
# ---------------------------------------------------------------------------
def test_run_py_imports_helper():
    """The root loop adapter imports the accounting helper."""
    src = ROOT_LOOP_PY.read_text()
    assert (
        'from lib.tasks_pkg.orchestrator._cache_round_accounting import'
        in src), (
        '_run.py must import the extracted cache-accounting helper — '
        'expected an `from lib.tasks_pkg.orchestrator.'
        '_cache_round_accounting import ...` line at module scope')
    assert 'stamp_round_cache_accounting' in src


def test_run_task_delegates_to_helper():
    """The stream loop's per-round cache-accounting cluster must be a
    single call to ``stamp_round_cache_accounting(task, ...)`` — no
    inline body left behind."""
    src = ROOT_LOOP_PY.read_text()
    assert 'stamp_round_cache_accounting(' in src, (
        '_run.py must call stamp_round_cache_accounting in the stream loop')


# ---------------------------------------------------------------------------
# 4. inline bodies are gone from _run.py (extraction really happened)
# ---------------------------------------------------------------------------
def test_run_py_no_longer_carries_detect_cache_break_call():
    """The inline ``detect_cache_break(`` call site moved into the leaf.
    (The IMPORT of detect_cache_break stays in _run.py for now — it is
    also imported by other extracted helpers; only the CALL site must
    be gone.)"""
    src = RUN_PY.read_text()
    code_lines = [
        ln for ln in src.splitlines()
        if not ln.lstrip().startswith('#')
    ]
    code = '\n'.join(code_lines)
    assert 'detect_cache_break(' not in code, (
        'detect_cache_break(...) call site must live in '
        '_cache_round_accounting.py, not _run.py — only the import '
        'may remain (used by other paths).')


def test_run_py_no_longer_carries_write_breakdown_call():
    """The inline ``_compute_write_breakdown(`` call site must have
    moved into the leaf too. Comments referencing it are permitted."""
    src = RUN_PY.read_text()
    code_lines = [
        ln for ln in src.splitlines()
        if not ln.lstrip().startswith('#')
    ]
    code = '\n'.join(code_lines)
    assert '_compute_write_breakdown(' not in code, (
        '_compute_write_breakdown(...) call site must live in '
        '_cache_round_accounting.py, not _run.py')


def test_run_py_no_longer_carries_log_round_cache_stats_call():
    """The inline ``log_round_cache_stats(`` call site must have moved
    into the leaf too."""
    src = RUN_PY.read_text()
    code_lines = [
        ln for ln in src.splitlines()
        if not ln.lstrip().startswith('#')
    ]
    code = '\n'.join(code_lines)
    assert 'log_round_cache_stats(' not in code, (
        'log_round_cache_stats(...) call site must live in '
        '_cache_round_accounting.py, not _run.py')


def test_run_py_no_longer_carries_cacheBreak_stamp():
    """The ``cacheBreak`` field stamping (assignment onto
    api_rounds[-1]) must have moved into the leaf."""
    src = RUN_PY.read_text()
    code_lines = [
        ln for ln in src.splitlines()
        if not ln.lstrip().startswith('#')
    ]
    code = '\n'.join(code_lines)
    assert "'cacheBreak'" not in code, (
        "The 'cacheBreak' string literal (used to stamp api_rounds[-1]) "
        'must live in _cache_round_accounting.py, not _run.py')


def test_run_py_no_longer_carries_writeBreakdown_stamp():
    """The ``writeBreakdown`` field stamping onto api_rounds[-1] must
    have moved into the leaf."""
    src = RUN_PY.read_text()
    code_lines = [
        ln for ln in src.splitlines()
        if not ln.lstrip().startswith('#')
    ]
    code = '\n'.join(code_lines)
    assert "'writeBreakdown'" not in code, (
        "The 'writeBreakdown' string literal (used to stamp "
        'api_rounds[-1]) must live in _cache_round_accounting.py, '
        'not _run.py')


# ---------------------------------------------------------------------------
# 5. leaf really carries the moved-out call sites + stamps
# ---------------------------------------------------------------------------
def test_leaf_carries_detect_cache_break_call():
    """The leaf must actually call detect_cache_break — a stealth
    NEUTER that deletes the call there would flip cache-break
    telemetry to blank."""
    src = LEAF_PY.read_text()
    assert 'detect_cache_break(' in src, (
        'stamp_round_cache_accounting must call detect_cache_break')


def test_leaf_carries_write_breakdown_call():
    """The leaf must call _compute_write_breakdown for the round-1
    baseline recovery + envelope decomposition."""
    src = LEAF_PY.read_text()
    assert '_compute_write_breakdown(' in src, (
        'stamp_round_cache_accounting must call _compute_write_breakdown')


def test_leaf_carries_log_round_cache_stats_call():
    """The leaf must issue the INFO-level per-round cache stats line —
    this is the production observability entry."""
    src = LEAF_PY.read_text()
    assert 'log_round_cache_stats(' in src, (
        'stamp_round_cache_accounting must call log_round_cache_stats')


def test_leaf_carries_all_three_stamped_fields():
    """The leaf must stamp all three fields (cacheBreak, toolCalls,
    writeBreakdown) — dropping any is a stealth telemetry regression."""
    src = LEAF_PY.read_text()
    for field in ("'cacheBreak'", "'toolCalls'", "'writeBreakdown'"):
        assert field in src, (
            f'stamp_round_cache_accounting must stamp {field}')


def test_leaf_carries_round_match_guard():
    """The leaf must guard the api_rounds[-1] stamping with a
    ``round == round_num + 1`` match — a bug that stamps the wrong
    round's entry is a hard-to-diagnose telemetry drift."""
    src = LEAF_PY.read_text()
    assert 'round_num + 1' in src or 'round_num+1' in src, (
        'stamp_round_cache_accounting must compare api_rounds[-1] '
        "['round'] against round_num+1 before stamping")


def test_leaf_carries_prev_turn_read_baseline():
    """Round-1 (len(api_rounds) < 2) must fetch the previous turn's
    cache_read via get_prev_turn_cache_read to seed the writeBreakdown
    envelope classification. Losing this baseline mis-classifies an
    evicted-tail re-bill as benign contextWrite."""
    src = LEAF_PY.read_text()
    assert 'get_prev_turn_cache_read' in src, (
        'stamp_round_cache_accounting must call '
        'get_prev_turn_cache_read to seed round-1 writeBreakdown')


# ---------------------------------------------------------------------------
# 6. behavioural: helper reproduces the inline mutations byte-for-byte
# ---------------------------------------------------------------------------
def test_helper_is_no_op_when_no_convId():
    """The inline body was guarded by ``if task.get('convId') and
    rs.last_usage``; the extracted helper must reproduce the same
    no-op behaviour for a task with no convId."""
    from lib.tasks_pkg.orchestrator._cache_round_accounting import (
        stamp_round_cache_accounting)
    api_rounds = [{'round': 1}]
    task = {'id': 'a' * 32, 'convId': '', '_userId': 1}
    stamp_round_cache_accounting(
        task, round_num=0, tid='abcd1234', model='claude-x',
        tools=None, usage={'input_tokens': 100, 'output_tokens': 20},
        assistant_msg={}, api_rounds=api_rounds, messages=[])
    assert 'cacheBreak' not in api_rounds[-1]
    assert 'writeBreakdown' not in api_rounds[-1]
    assert 'toolCalls' not in api_rounds[-1]


def test_helper_is_no_op_when_no_usage():
    """Same guard — no usage → no stamping."""
    from lib.tasks_pkg.orchestrator._cache_round_accounting import (
        stamp_round_cache_accounting)
    api_rounds = [{'round': 1}]
    task = {'id': 'a' * 32, 'convId': 'conv-x', '_userId': 1}
    stamp_round_cache_accounting(
        task, round_num=0, tid='abcd1234', model='claude-x',
        tools=None, usage=None,
        assistant_msg={}, api_rounds=api_rounds, messages=[])
    assert 'cacheBreak' not in api_rounds[-1]
    assert 'writeBreakdown' not in api_rounds[-1]
    assert 'toolCalls' not in api_rounds[-1]


def test_helper_stamps_toolCalls_names_from_assistant_msg():
    """When the assistant emitted tool_calls, their function names
    must land on api_rounds[-1]['toolCalls'] as a list."""
    from lib.tasks_pkg.orchestrator._cache_round_accounting import (
        stamp_round_cache_accounting)
    api_rounds = [{'round': 1}]
    task = {'id': 'a' * 32, 'convId': 'conv-x', '_userId': 1}
    assistant_msg = {
        'role': 'assistant',
        'content': '',
        'tool_calls': [
            {'id': 'tc_1', 'function': {'name': 'search_web'}},
            {'id': 'tc_2', 'function': {'name': 'read_file'}},
        ],
    }
    stamp_round_cache_accounting(
        task, round_num=0, tid='abcd1234', model='claude-x',
        tools=None,
        usage={'input_tokens': 100, 'output_tokens': 20,
               'cache_read_input_tokens': 0,
               'cache_creation_input_tokens': 0},
        assistant_msg=assistant_msg,
        api_rounds=api_rounds, messages=[])
    assert api_rounds[-1].get('toolCalls') == ['search_web', 'read_file']


def test_helper_skips_stamp_when_round_number_mismatch():
    """If api_rounds[-1] belongs to a different round (e.g. this round
    produced no usage so no entry was appended), the helper must NOT
    scribble on it."""
    from lib.tasks_pkg.orchestrator._cache_round_accounting import (
        stamp_round_cache_accounting)
    # api_rounds[-1].round == 5, but we're stamping round_num=0
    # (⇒ round_num + 1 = 1). Mismatch.
    api_rounds = [{'round': 5}]
    task = {'id': 'a' * 32, 'convId': 'conv-x', '_userId': 1}
    stamp_round_cache_accounting(
        task, round_num=0, tid='abcd1234', model='claude-x',
        tools=None,
        usage={'input_tokens': 100, 'output_tokens': 20,
               'cache_read_input_tokens': 0,
               'cache_creation_input_tokens': 0},
        assistant_msg={'tool_calls': [
            {'id': 't', 'function': {'name': 'x'}}]},
        api_rounds=api_rounds, messages=[])
    assert 'toolCalls' not in api_rounds[-1]
    assert 'writeBreakdown' not in api_rounds[-1]
    assert 'cacheBreak' not in api_rounds[-1]
