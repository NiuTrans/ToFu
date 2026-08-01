"""Wire-parity guards for pt_03f4cdf1 slice 14 — extract the
malformed-tool_call-arguments JSON sanitizer cluster from
_run.py's stream loop into
lib.tasks_pkg.orchestrator._sanitize_tool_call_args
    .sanitize_malformed_tool_call_args().

The cluster runs RIGHT AFTER ``parse_tool_calls`` returns. For any
``parsed_tcs`` entry whose ``args_parse_err`` is non-None, the cluster:

    * Finds the matching ``tool_calls[i]`` on ``messages[-1]`` by tc_id.
    * Rewrites its ``function.arguments`` to ``'{}'`` — the error
      tool_result already teaches the model what went wrong, and the
      gateway now sees valid JSON on the next round instead of a HTTP
      400 ``invalid function arguments json string``.
    * Logs an INFO line with the sanitized args length and the RAW
      bad args (truncated at 600 chars) so 2026-07-27-style
      concatenated-tool-name postmortems still have the evidence.

The cluster is a pure mutation of ``messages[-1]['tool_calls'][i]
['function']['arguments']`` — no early-exit control flow, no
loop-mutation. That is why it is a clean cut.

Failing-first: this file is written BEFORE the extraction lands.
Each guard turns RED until the extraction really happens and the
delegation call replaces the inline body in _run.py.
"""

from __future__ import annotations

import importlib
import inspect
import logging
import pathlib


ROOT = pathlib.Path(__file__).resolve().parents[1]
RUN_PY = ROOT / 'lib' / 'tasks_pkg' / 'orchestrator' / '_run.py'
LEAF_PY = (
    ROOT / 'lib' / 'tasks_pkg' / 'orchestrator' /
    '_sanitize_tool_call_args.py')
# Slice 22 (2026-07-31) moved the sanitize call site out of _run.py
# into the tool-dispatch cluster leaf: parse → sanitize → emit →
# execute now lives in _tool_dispatch_round.run_tool_dispatch, and
# _run.py delegates the whole cluster. The two wiring guards below
# therefore assert on the dispatch leaf, not _run.py.
DISPATCH_PY = (
    ROOT / 'lib' / 'tasks_pkg' / 'orchestrator' /
    '_tool_dispatch_round.py')


# ---------------------------------------------------------------------------
# 1. leaf module exists and exposes the helper by name
# ---------------------------------------------------------------------------
def test_leaf_module_exists_and_exposes_helper():
    """The new leaf ships a single top-level callable named
    ``sanitize_malformed_tool_call_args`` — the seam name run_task
    delegates to."""
    mod = importlib.import_module(
        'lib.tasks_pkg.orchestrator._sanitize_tool_call_args')
    assert hasattr(mod, 'sanitize_malformed_tool_call_args'), (
        'lib.tasks_pkg.orchestrator._sanitize_tool_call_args must export '
        'sanitize_malformed_tool_call_args')
    assert callable(mod.sanitize_malformed_tool_call_args)


# ---------------------------------------------------------------------------
# 2. helper signature — kw-only diagnostic scalars
# ---------------------------------------------------------------------------
def test_helper_signature_is_keyword_only():
    """Positional: parsed_tcs, messages. Kw-only diagnostics:
    tid, conv_id, model. A signature drift breaks the call site."""
    from lib.tasks_pkg.orchestrator._sanitize_tool_call_args import (
        sanitize_malformed_tool_call_args)
    sig = inspect.signature(sanitize_malformed_tool_call_args)
    params = sig.parameters
    assert 'parsed_tcs' in params
    assert 'messages' in params
    for name in ('tid', 'conv_id', 'model'):
        assert name in params, (
            f'sanitize_malformed_tool_call_args must accept {name}')
        assert params[name].kind == inspect.Parameter.KEYWORD_ONLY, (
            f'{name} must be keyword-only')


# ---------------------------------------------------------------------------
# 3. _run.py imports and delegates to the extracted helper
# ---------------------------------------------------------------------------
def test_run_py_imports_helper():
    """The dispatch-round leaf imports sanitize_malformed_tool_call_args
    at module scope. (Was _run.py before slice 22 moved the cluster.)"""
    src = DISPATCH_PY.read_text()
    assert (
        'from lib.tasks_pkg.orchestrator._sanitize_tool_call_args import'
        in src), (
        '_tool_dispatch_round.py must import the extracted sanitizer '
        'helper — expected an `from lib.tasks_pkg.orchestrator.'
        '_sanitize_tool_call_args import ...` line at module scope')
    assert 'sanitize_malformed_tool_call_args' in src


def test_run_task_delegates_to_helper():
    """The dispatch cluster must call
    ``sanitize_malformed_tool_call_args(parsed_tcs, messages, ...)`` —
    no inline body left behind. (Call site moved from _run.py's run_task
    to _tool_dispatch_round.run_tool_dispatch in slice 22.)"""
    src = DISPATCH_PY.read_text()
    assert 'sanitize_malformed_tool_call_args(' in src, (
        '_tool_dispatch_round.py must call sanitize_malformed_tool_call_args '
        'in the dispatch cluster')


# ---------------------------------------------------------------------------
# 4. inline bodies are gone from _run.py (extraction really happened)
# ---------------------------------------------------------------------------
def test_run_py_no_longer_carries_args_parse_err_loop():
    """The inline ``for ... in parsed_tcs`` loop that inspected
    ``args_parse_err`` must have moved out. (The name ``parsed_tcs``
    itself stays — it is the return of parse_tool_calls and is fed
    into other helpers like emit_tool_exec_phase / execute_tool_pipeline.)"""
    src = RUN_PY.read_text()
    code_lines = [
        ln for ln in src.splitlines()
        if not ln.lstrip().startswith('#')
    ]
    code = '\n'.join(code_lines)
    assert 'args_parse_err' not in code, (
        "The 'args_parse_err' inline loop must live in "
        '_sanitize_tool_call_args.py, not _run.py')


def test_run_py_no_longer_carries_sanitize_arguments_write():
    """The inline write of ``fn['arguments'] = '{}'`` must be gone
    from _run.py (comments referencing it are permitted)."""
    src = RUN_PY.read_text()
    code_lines = [
        ln for ln in src.splitlines()
        if not ln.lstrip().startswith('#')
    ]
    code = '\n'.join(code_lines)
    assert "'arguments'] = '{}'" not in code, (
        "The fn['arguments'] = '{}' sanitizer write must live in "
        '_sanitize_tool_call_args.py, not _run.py')


# ---------------------------------------------------------------------------
# 5. leaf really carries the moved-out logic
# ---------------------------------------------------------------------------
def test_leaf_carries_args_parse_err_gate():
    """Extraction is real — the leaf inspects args_parse_err."""
    src = LEAF_PY.read_text()
    assert 'args_parse_err' in src, (
        'sanitize_malformed_tool_call_args must gate on args_parse_err')


def test_leaf_carries_arguments_write():
    """The leaf must overwrite ``function.arguments`` to a valid JSON
    empty object. A stealth NEUTER that dropped the write would flip
    the next round back to HTTP 400."""
    src = LEAF_PY.read_text()
    assert "'arguments'] = '{}'" in src, (
        'sanitize_malformed_tool_call_args must write '
        "fn['arguments'] = '{}'")


def test_leaf_matches_tool_call_by_tc_id():
    """The leaf must find the matching live tool_call by tc_id — a
    naive positional match would silently corrupt a round with
    multiple parallel tool_calls where only one had bad args."""
    src = LEAF_PY.read_text()
    assert "'id') != tc_id" in src or "'id') == tc_id" in src, (
        'sanitize_malformed_tool_call_args must match by tc_id')


def test_leaf_carries_raw_args_log():
    """The leaf must keep the RAW args INFO log line (truncated at
    600 chars) — that is the decisive evidence for
    concatenated-tool-name postmortems."""
    src = LEAF_PY.read_text()
    assert 'bad_args[:600]' in src or "[:600]" in src, (
        'sanitize_malformed_tool_call_args must keep the RAW args '
        'truncation at 600 chars')


# ---------------------------------------------------------------------------
# 6. behavioural: helper reproduces the inline mutations byte-for-byte
# ---------------------------------------------------------------------------
def _make_parsed_tc(*, tc_id: str, fn_name: str, args_parse_err):
    """Emulate parse_tool_calls' 7-tuple shape."""
    return (
        {'id': tc_id, 'function': {'name': fn_name}},  # tc
        fn_name,                                        # fn_name
        tc_id,                                          # tc_id
        {},                                             # fn_args (parsed)
        1,                                              # rn (tool_round_num)
        None,                                           # round_entry
        args_parse_err,                                 # args_parse_err
    )


def test_helper_sanitizes_bad_args_to_empty_object():
    """When a tc has args_parse_err, its live arguments in
    messages[-1] must be rewritten to ``'{}'``."""
    from lib.tasks_pkg.orchestrator._sanitize_tool_call_args import (
        sanitize_malformed_tool_call_args)
    messages = [{
        'role': 'assistant',
        'content': '',
        'tool_calls': [{
            'id': 'tc_bad',
            'function': {'name': 'grep', 'arguments': r'{"pattern":"\d"}'},
        }],
    }]
    parsed = [_make_parsed_tc(
        tc_id='tc_bad', fn_name='grep',
        args_parse_err='Invalid \\ escape')]
    sanitize_malformed_tool_call_args(
        parsed, messages, tid='abcd1234', conv_id='conv-x',
        model='claude-x')
    assert messages[-1]['tool_calls'][0]['function']['arguments'] == '{}'


def test_helper_leaves_good_args_untouched():
    """A tc with args_parse_err=None must not be touched — its
    live arguments payload is real user data."""
    from lib.tasks_pkg.orchestrator._sanitize_tool_call_args import (
        sanitize_malformed_tool_call_args)
    good_args = '{"path":"/etc/hosts"}'
    messages = [{
        'role': 'assistant',
        'content': '',
        'tool_calls': [{
            'id': 'tc_good',
            'function': {'name': 'read_files', 'arguments': good_args},
        }],
    }]
    parsed = [_make_parsed_tc(
        tc_id='tc_good', fn_name='read_files', args_parse_err=None)]
    sanitize_malformed_tool_call_args(
        parsed, messages, tid='abcd1234', conv_id='conv-x',
        model='claude-x')
    assert messages[-1]['tool_calls'][0]['function']['arguments'] == good_args


def test_helper_matches_by_tc_id_not_position():
    """When multiple parallel tool_calls exist and only one has bad
    args, only THAT one must be rewritten. Rewriting the wrong slot
    would silently corrupt a user's real request."""
    from lib.tasks_pkg.orchestrator._sanitize_tool_call_args import (
        sanitize_malformed_tool_call_args)
    good_args = '{"path":"/etc/hosts"}'
    bad_args = r'{"pattern":"\d"}'
    messages = [{
        'role': 'assistant',
        'content': '',
        'tool_calls': [
            {'id': 'tc_good', 'function': {
                'name': 'read_files', 'arguments': good_args}},
            {'id': 'tc_bad', 'function': {
                'name': 'grep', 'arguments': bad_args}},
        ],
    }]
    parsed = [
        _make_parsed_tc(
            tc_id='tc_good', fn_name='read_files', args_parse_err=None),
        _make_parsed_tc(
            tc_id='tc_bad', fn_name='grep',
            args_parse_err='Invalid \\ escape'),
    ]
    sanitize_malformed_tool_call_args(
        parsed, messages, tid='abcd1234', conv_id='conv-x',
        model='claude-x')
    assert messages[-1]['tool_calls'][0]['function']['arguments'] == good_args
    assert messages[-1]['tool_calls'][1]['function']['arguments'] == '{}'


def test_helper_is_no_op_on_empty_parsed_tcs():
    """No parsed tcs → no work → no mutation, no exception."""
    from lib.tasks_pkg.orchestrator._sanitize_tool_call_args import (
        sanitize_malformed_tool_call_args)
    messages = [{'role': 'assistant', 'content': 'hi'}]
    before = messages[-1].copy()
    sanitize_malformed_tool_call_args(
        [], messages, tid='abcd1234', conv_id='conv-x', model='claude-x')
    assert messages[-1] == before


def test_helper_is_no_op_when_no_messages():
    """Empty messages list must not crash — the inline code guarded
    with ``messages[-1] if messages else {}``."""
    from lib.tasks_pkg.orchestrator._sanitize_tool_call_args import (
        sanitize_malformed_tool_call_args)
    parsed = [_make_parsed_tc(
        tc_id='tc_x', fn_name='y', args_parse_err='bad')]
    # Must not raise IndexError.
    sanitize_malformed_tool_call_args(
        parsed, [], tid='abcd1234', conv_id='conv-x', model='claude-x')


def test_helper_emits_info_log_with_raw_args(caplog):
    """The RAW bad args must land in the INFO log (truncated at 600
    chars). This is the decisive line for concatenated-tool-name
    postmortems — losing it means a class of production bugs becomes
    invisible."""
    from lib.tasks_pkg.orchestrator._sanitize_tool_call_args import (
        sanitize_malformed_tool_call_args)
    bad_args = r'{"pattern":"\d"}'
    messages = [{
        'role': 'assistant',
        'content': '',
        'tool_calls': [{
            'id': 'tc_bad',
            'function': {'name': 'grep', 'arguments': bad_args},
        }],
    }]
    parsed = [_make_parsed_tc(
        tc_id='tc_bad', fn_name='grep',
        args_parse_err='Invalid \\ escape')]
    caplog.set_level(logging.INFO)
    sanitize_malformed_tool_call_args(
        parsed, messages, tid='abcd1234', conv_id='conv-x',
        model='claude-x')
    text = ' '.join(rec.getMessage() for rec in caplog.records)
    assert 'raw malformed args' in text.lower() or 'raw' in text.lower(), (
        'sanitize helper must INFO-log the raw bad args')
