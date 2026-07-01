"""Regression tests for the paper report/Q&A tool-argument repair seam.

Root-cause coverage for the "507 searches, mostly punctuation" bug: a model
that emits ``web_search`` with ``queries`` as a bare STRING (a schema
violation — the schema wants an array of ``{query}`` objects) used to be
iterated character-by-character by both the display label and the executor,
firing one web search per character.

The fix routes the paper agents' raw ``arguments`` through the SAME canonical
seams chat mode uses — no paper-specific reimplementation:
  * ``lib.tool_input_repair.parse_and_repair_tool_args`` (decode + repair),
  * ``lib.tasks_pkg.tool_display.tool_round_label`` (the label chat renders).
``lib.paper.tools`` re-exports both (``parse_and_repair_tool_args`` /
``display_query_for``) for the report + Q&A engines. This suite proves:

  1. a bare-string ``queries`` is coerced to a single-element array (1 search,
     not N);
  2. the shared label renders the real query text for BOTH dict and
     coerced-string entries (never an empty ``N searches:``);
  3. the executor issues exactly ONE search for the bug payload (no per-char
     iteration), with the network stubbed;
  4. well-formed batch / single / fetch_url inputs are unchanged;
  5. the paper label helper IS chat's ``tool_round_label`` (consolidation,
     not a fork).
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import lib.paper.tools as T  # noqa: E402
from lib.paper.tools import (  # noqa: E402
    display_query_for,
    parse_and_repair_tool_args,
)


# ── (1) bare-string queries → single-element array ────────────────────────

def test_bare_string_queries_coerced_to_single_array():
    """The exact bug: a long string in the ``queries`` slot."""
    payload = json.dumps({'queries': 'Agentic Rubrics SWE follow-ups 2025' + ('. ' * 240)})
    fn_args, log = parse_and_repair_tool_args('web_search', payload)
    assert isinstance(fn_args['queries'], list)
    assert len(fn_args['queries']) == 1  # NOT 507
    assert ('queries', 'bare_string_to_array') in log


def test_bare_string_queries_label_is_not_empty():
    """The screenshot symptom: ``507 searches:  +504 more`` with NO previews.

    A coerced single-element array must render as the real query text, a
    single search — never the empty-preview multi-search label.
    """
    payload = json.dumps({'queries': 'follow-up work on contextual verifiers'})
    fn_args, _ = parse_and_repair_tool_args('web_search', payload)
    label = display_query_for('web_search', fn_args)
    assert label == 'follow-up work on contextual verifiers'
    assert 'searches:' not in label  # not the multi-search label


# ── (2) display_query_for handles dict AND string entries ──────────────────

def test_display_label_multi_dict_queries():
    fn_args = {'queries': [{'query': 'foo bar'}, {'query': 'baz qux'},
                           {'query': 'a'}, {'query': 'b'}]}
    # Chat's canonical multi-line batch label (one query per line).
    label = display_query_for('web_search', fn_args)
    assert label == '4 searches:\n• foo bar\n• baz qux\n• a\n• b'


def test_display_label_mixed_string_and_dict_entries():
    """After repair an entry may be a plain string — must still preview."""
    fn_args = {'queries': ['plain string query', {'query': 'dict query'}]}
    label = display_query_for('web_search', fn_args)
    assert label == '2 searches:\n• plain string query\n• dict query'


def test_display_label_single_query_field():
    fn_args = {'query': 'single query here'}
    assert display_query_for('web_search', fn_args) == 'single query here'


def test_display_label_fetch_url_single_string():
    fn_args, _ = parse_and_repair_tool_args(
        'fetch_url', json.dumps({'urls': 'https://arxiv.org/abs/2509.12345'}))
    assert len(fn_args['urls']) == 1
    # Chat shortens the URL for display (host + path, scheme dropped) and
    # exposes the full value via _batchUrls — that's the canonical behaviour.
    assert display_query_for('fetch_url', fn_args) == 'arxiv.org/abs/2509.12345'


# ── (3) executor issues ONE search for the bug payload ─────────────────────

def test_executor_issues_one_search_for_bare_string(monkeypatch):
    calls = []

    def fake_web_search_one(q, user_question, f, vertical='auto'):
        calls.append(q)
        return ([], None, None, None)

    monkeypatch.setattr(T, '_web_search_one', fake_web_search_one)
    monkeypatch.setattr(T, 'format_search_for_tool_response',
                        lambda results, search_diag=None: 'FORMATTED')
    monkeypatch.setattr(T, '_format_search_display_for_results', lambda results: [])

    payload = json.dumps({'queries': 'a,b.c;d!e?f' * 10})  # 110-char string
    content, display, diag, eng, verts = T._execute_report_tool('web_search', payload)
    assert len(calls) == 1  # exactly one search, NOT one-per-character
    assert content == 'FORMATTED'


def test_executor_runs_real_batch(monkeypatch):
    """A genuine multi-query batch still fans out to N searches (≤5 cap)."""
    calls = []

    def fake_web_search_one(q, user_question, f, vertical='auto'):
        calls.append(q)
        return ([], None, None, None)

    monkeypatch.setattr(T, '_web_search_one', fake_web_search_one)
    monkeypatch.setattr(T, 'format_search_for_tool_response',
                        lambda results, search_diag=None: 'F')
    monkeypatch.setattr(T, '_format_search_display_for_results', lambda results: [])

    payload = json.dumps({'queries': [{'query': 'q1'}, {'query': 'q2'}, {'query': 'q3'}]})
    T._execute_report_tool('web_search', payload)
    assert sorted(calls) == ['q1', 'q2', 'q3']


# ── (4) parse helper never raises on garbage ───────────────────────────────

def test_parse_helper_bad_json_returns_empty():
    fn_args, log = parse_and_repair_tool_args('web_search', '{not valid json')
    assert fn_args == {}
    assert log == []


def test_parse_helper_non_dict_returns_empty():
    fn_args, log = parse_and_repair_tool_args('web_search', json.dumps(['a', 'b']))
    assert fn_args == {}
    assert log == []


# ── (5) consolidation: paper helpers ARE chat's, not a fork ────────────────

def test_paper_helpers_are_chat_canonical():
    """The paper module must RE-EXPORT chat's seams, never reimplement them."""
    from lib.tasks_pkg.tool_display import tool_round_label
    from lib.tool_input_repair import (
        parse_and_repair_tool_args as canonical_parse,
    )
    assert display_query_for is tool_round_label
    assert parse_and_repair_tool_args is canonical_parse


if __name__ == '__main__':
    import traceback

    class _MP:
        def __init__(self):
            self._undo = []

        def setattr(self, obj, name, val):
            self._undo.append((obj, name, getattr(obj, name)))
            setattr(obj, name, val)

        def undo(self):
            for obj, name, val in reversed(self._undo):
                setattr(obj, name, val)
            self._undo = []

    passed = failed = 0
    for name, fn in list(globals().items()):
        if name.startswith('test_') and callable(fn):
            mp = _MP()
            try:
                if 'monkeypatch' in fn.__code__.co_varnames[:fn.__code__.co_argcount]:
                    fn(mp)
                else:
                    fn()
                passed += 1
                print(f'PASS {name}')
            except Exception:
                failed += 1
                print(f'FAIL {name}')
                traceback.print_exc()
            finally:
                mp.undo()
    print(f'\n{passed} passed, {failed} failed')
    sys.exit(0 if failed == 0 else 1)
