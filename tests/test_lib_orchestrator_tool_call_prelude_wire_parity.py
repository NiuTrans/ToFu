# Incident anchor: born in commit 0d3f59bb — refactor(orchestrator): pt_03f4cdf1 slice 16 — extract live-tail tool...
# (funeral audit pt_c565a36b3e8f42e6, docs/RATCHET_AUDIT.md)
"""Wire-parity guards for pt_03f4cdf1 slice 16 — extract the live-tail
assistant/tool_call assembly + inter-round narration discard +
incremental auto-translate submit cluster from _run.py's stream loop
into
lib.tasks_pkg.orchestrator._tool_call_prelude
    .append_assistant_tool_call_message().

The cluster runs after the per-round budget gate and before the abort check.
Three sequential mutations that all belong together — they are
the "we've decided to call tools this round" bracket:

    * Assemble the live-tail assistant/tool_call message through the
      SHARED build_assistant_tool_call_message (same as the replay
      path) — the SINGLE SOURCE guarantee that keeps live vs replay
      structurally identical (WIRE PREFIX CHANGED miss avoidance).
    * Discard the inter-round narration this round streamed before
      its tool calls (_discard_pretool_prose) so the backend and
      client agree on where "prose ended, tool started".
    * Submit an incremental auto-translate for this round's prose
      segment (via lib.translate.submit_round_segment) so the
      Chinese lands by task end instead of one giant stall.

None of the three cross an early-exit; the whole cluster is a pure
mutation prelude to tool execution.

Failing-first: written BEFORE the extraction lands. Each guard turns
RED until the extraction really happens and the delegation call
replaces the inline body in _run.py.
"""

from __future__ import annotations

import importlib
import inspect
import pathlib


ROOT = pathlib.Path(__file__).resolve().parents[1]
RUN_PY = ROOT / 'lib' / 'tasks_pkg' / 'orchestrator' / '_run.py'
LEAF_PY = (
    ROOT / 'lib' / 'tasks_pkg' / 'orchestrator' /
    '_tool_call_prelude.py')


# ---------------------------------------------------------------------------
# 1. leaf module exists and exposes the helper by name
# ---------------------------------------------------------------------------
def test_leaf_module_exists_and_exposes_helper():
    mod = importlib.import_module(
        'lib.tasks_pkg.orchestrator._tool_call_prelude')
    assert hasattr(mod, 'append_assistant_tool_call_message'), (
        'lib.tasks_pkg.orchestrator._tool_call_prelude must export '
        'append_assistant_tool_call_message')
    assert callable(mod.append_assistant_tool_call_message)


# ---------------------------------------------------------------------------
# 2. helper signature — kw-only scalars so callers can't get order wrong
# ---------------------------------------------------------------------------
def test_helper_signature_is_keyword_only():
    """The helper takes ``task`` and ``messages`` positional and the
    round-scoped scalars kw-only (round_num, tid, assistant_msg)."""
    from lib.tasks_pkg.orchestrator._tool_call_prelude import (
        append_assistant_tool_call_message)
    sig = inspect.signature(append_assistant_tool_call_message)
    params = sig.parameters
    assert 'task' in params
    assert 'messages' in params
    for name in ('round_num', 'tid', 'assistant_msg'):
        assert name in params, (
            f'append_assistant_tool_call_message must accept {name}')
        assert params[name].kind == inspect.Parameter.KEYWORD_ONLY, (
            f'{name} must be keyword-only')


# ---------------------------------------------------------------------------
# 3. _run.py imports + delegates to the extracted helper
# ---------------------------------------------------------------------------
def test_run_py_imports_helper():
    src = RUN_PY.read_text()
    assert (
        'from lib.tasks_pkg.orchestrator._tool_call_prelude import'
        in src), (
        '_run.py must import the extracted helper — expected a '
        '`from lib.tasks_pkg.orchestrator._tool_call_prelude import ...` '
        'line at module scope')
    assert 'append_assistant_tool_call_message' in src


def test_run_task_delegates_to_helper():
    src = RUN_PY.read_text()
    assert 'append_assistant_tool_call_message(' in src, (
        '_run.py must call append_assistant_tool_call_message in the '
        'stream loop')


# ---------------------------------------------------------------------------
# 4. inline bodies are gone from _run.py (extraction really happened)
# ---------------------------------------------------------------------------
def test_run_py_no_longer_carries_build_assistant_tool_call_message_call():
    """The inline ``build_assistant_tool_call_message(`` call site must
    have moved into the leaf (the SHARED replay-path builder still
    lives in conv_message_builder — only the CALL SITE moved)."""
    src = RUN_PY.read_text()
    code_lines = [
        ln for ln in src.splitlines()
        if not ln.lstrip().startswith('#')
    ]
    code = '\n'.join(code_lines)
    assert 'build_assistant_tool_call_message(' not in code, (
        'build_assistant_tool_call_message(...) call site must live in '
        '_tool_call_prelude.py, not _run.py')


def test_run_py_no_longer_carries_discard_pretool_prose_call():
    """The inline ``_discard_pretool_prose(`` call site must have moved
    into the leaf. The IMPORT stays because the helper still delegates
    to _discard_pretool_prose from _finalize."""
    src = RUN_PY.read_text()
    code_lines = [
        ln for ln in src.splitlines()
        if not ln.lstrip().startswith('#')
    ]
    code = '\n'.join(code_lines)
    assert '_discard_pretool_prose(' not in code, (
        '_discard_pretool_prose(...) call site must live in '
        '_tool_call_prelude.py, not _run.py')


def test_run_py_no_longer_carries_submit_round_segment_call():
    """The inline ``submit_round_segment(`` call must have moved into
    the leaf. The IMPORT stays only if it is used elsewhere; if not,
    the leaf owns it exclusively."""
    src = RUN_PY.read_text()
    code_lines = [
        ln for ln in src.splitlines()
        if not ln.lstrip().startswith('#')
    ]
    code = '\n'.join(code_lines)
    assert 'submit_round_segment(' not in code, (
        'submit_round_segment(...) call site must live in '
        '_tool_call_prelude.py, not _run.py')


# ---------------------------------------------------------------------------
# 5. leaf really carries the moved-out call sites
# ---------------------------------------------------------------------------
def test_leaf_carries_build_assistant_tool_call_message_call():
    src = LEAF_PY.read_text()
    assert 'build_assistant_tool_call_message(' in src, (
        'append_assistant_tool_call_message must call '
        'build_assistant_tool_call_message — the SHARED replay-path '
        'builder that guarantees live vs replay structural identity')


def test_leaf_carries_discard_pretool_prose_call():
    src = LEAF_PY.read_text()
    assert '_discard_pretool_prose(' in src, (
        'append_assistant_tool_call_message must call '
        '_discard_pretool_prose so the backend and client agree on '
        'where prose ended and tools started')


def test_leaf_carries_submit_round_segment_call():
    src = LEAF_PY.read_text()
    assert 'submit_round_segment(' in src, (
        'append_assistant_tool_call_message must submit an incremental '
        'auto-translate — a stealth stub would defer translation to the '
        'end-of-task stall')


def test_leaf_swallows_translate_exception():
    """The submit_round_segment call is inside a try/except (translate
    is non-fatal). A stealth removal that lets ImportError abort the
    LLM round is a serious regression."""
    src = LEAF_PY.read_text()
    # The characteristic 'non-fatal' log line stays as evidence of the
    # try/except contract.
    assert 'try:' in src, (
        'append_assistant_tool_call_message must try/except the '
        'incremental-translate submit')
    assert 'submit_round_segment' in src and 'except' in src, (
        'The submit_round_segment call must be wrapped in a try/except '
        'so a translate-side ImportError never breaks the LLM round')


def test_leaf_stamps_compact_messages():
    """After tool_calls are appended, _run.py stamps
    task['_compact_messages'] = messages so context_compact tool
    handler sees the live list. Losing this stamp breaks the
    context_compact tool."""
    src = LEAF_PY.read_text()
    assert "_compact_messages" in src, (
        'append_assistant_tool_call_message must stamp '
        "task['_compact_messages'] = messages after the tool_call "
        'append — context_compact tool depends on this')


# ---------------------------------------------------------------------------
# 6. behavioural: helper reproduces the inline mutations byte-for-byte
# ---------------------------------------------------------------------------
def _make_task(**overrides):
    """Build a synthetic task dict with the fields _discard_pretool_prose
    and other helpers touch. Real run_task creates content_lock in setup."""
    import threading
    t = {
        'id': 'a' * 32, 'convId': 'c1',
        'content_lock': threading.RLock(),
        'segments': [],
        # Fields _discard_pretool_prose may read/write:
        'content': '', 'thinking': '',
    }
    t.update(overrides)
    return t


def test_helper_appends_shared_builder_output(monkeypatch):
    """The helper must delegate to build_assistant_tool_call_message
    and append its return value onto ``messages``. Verify by
    monkeypatching the LEAF's module-level ref (leaf imported the
    builder at module scope)."""
    import lib.tasks_pkg.orchestrator._tool_call_prelude as mod

    sentinel = {'role': 'assistant', 'content': '',
                'tool_calls': [{'id': 'tc_1'}]}

    def _fake_builder(*, tool_calls, content, reasoning_content,
                      thinking_signature):
        # Sanity: the caller must forward all 4 kw-args.
        return sentinel

    # Patch through the LEAF's module scope (where it was imported).
    monkeypatch.setattr(
        mod, 'build_assistant_tool_call_message', _fake_builder)
    # Stub the other two side-effect calls the leaf makes.
    monkeypatch.setattr(mod, '_discard_pretool_prose', lambda *_a: None)
    import lib.translate as _tr
    monkeypatch.setattr(_tr, 'submit_round_segment', lambda *_a: None)

    task = _make_task()
    messages = [{'role': 'user', 'content': 'hi'}]
    assistant_msg = {
        'role': 'assistant', 'content': 'thinking...',
        'tool_calls': [{'id': 'tc_1'}],
        'reasoning_content': None, 'thinking_signature': None,
    }
    mod.append_assistant_tool_call_message(
        task, messages,
        round_num=0, tid='abcd', assistant_msg=assistant_msg)

    assert messages[-1] is sentinel, (
        'helper must append the shared builder\'s return value '
        'to messages')
    assert task.get('_compact_messages') is messages, (
        'helper must stamp task[_compact_messages] = messages')


def test_helper_translate_failure_is_swallowed(monkeypatch):
    """A translate-side crash must NOT bubble — an inspector or
    translate regression cannot break the LLM round."""
    import lib.tasks_pkg.orchestrator._tool_call_prelude as mod

    class _Boom(Exception):
        pass

    def _boom(*_a, **_k):
        raise _Boom('translate failed')

    # Stub _discard_pretool_prose too so we isolate the translate path.
    monkeypatch.setattr(mod, '_discard_pretool_prose', lambda *_a: None)
    import lib.translate as _tr
    monkeypatch.setattr(_tr, 'submit_round_segment', _boom)

    task = _make_task()
    messages = [{'role': 'user', 'content': 'hi'}]
    assistant_msg = {
        'role': 'assistant', 'content': 'x',
        'tool_calls': [{'id': 'tc_1', 'function': {'name': 'grep'}}],
        'reasoning_content': None, 'thinking_signature': None,
    }
    # Must not raise.
    mod.append_assistant_tool_call_message(
        task, messages,
        round_num=0, tid='abcd', assistant_msg=assistant_msg)
