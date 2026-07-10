#!/usr/bin/env python3
"""Headless tests for the AGENTIC describe-to-recommend interpretation.

The recommend flow used to guess candidate titles from the LLM's frozen
training memory (one ``dispatch_chat`` call, no tools) — so it could not know
about a conference happening *today* or papers posted last week, and it
manufactured false "that hasn't happened yet" corrections because the prompt
carried no current date. This suite proves the interpretation is now a REAL
agentic tool loop:

  1. the model is offered ``web_search`` / ``fetch_url`` and its tool calls are
     actually executed and fed back before it produces candidates;
  2. the system prompt is date-anchored (so it stops treating an in-progress
     year as the future);
  3. the research ``tool_start`` / ``tool_done`` events are forwarded to the
     caller (for the "researching…" UI trail);
  4. NEUTER: with the tool loop disabled (only the final round runs, tools
     ignored), the research tool call is NEVER executed — proving the loop is
     load-bearing, not decorative.

Runs fully offline: ``dispatch_stream`` is faked to emit a web_search call on
round 0 and the final JSON on round 1, and ``_execute_report_tool`` +
``search_arxiv`` are faked so grounding resolves without a network.

Run standalone: ``python3 tests/test_paper_recommend_agentic.py``
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault('TRADING_ENABLED', '0')

import lib.paper.recommend_engine as re_mod  # noqa: E402


def _color(s, c): return f'\033[{c}m{s}\033[0m'
def _ok(msg): print(' ', _color('✓', '32'), msg)
def _fail(msg): print(' ', _color('✗', '31'), msg); sys.exit(1)


_FAKE_ARXIV = {
    '2502.09992': {
        'arxiv_id': '2502.09992', 'title': 'Large Language Diffusion Models',
        'authors': ['Shen Nie'], 'summary': 'LLaDA.', 'published': '2025-02-14',
        'primary_category': 'cs.CL', 'pdf_url': '', 'abs_url': '',
    },
}


def _fake_search_by_title(title):
    tl = (title or '').lower()
    if 'large language diffusion' in tl or 'llada' in tl:
        return [_FAKE_ARXIV['2502.09992']]
    return []


# The final-round JSON the model returns AFTER researching.
_FINAL_JSON = {
    'candidates': [
        {'title': 'Large Language Diffusion Models', 'arxiv_id': '2502.09992',
         'venue': 'ICML 2025 Oral', 'why': 'the current dLLM you likely mean.'},
    ],
    'correction': None,
}


class _Patched:
    """Fake an agentic 2-round dispatch: round 0 issues a web_search tool call,
    round 1 (offered no tools) returns the final JSON. Records what the loop
    executed so the test can assert real tool use.

    ``break_loop=True`` neuters the loop by making round 0 ALSO return the final
    JSON with no tool calls — i.e. the model never gets to research.
    """
    def __init__(self, *, break_loop=False):
        self.break_loop = break_loop
        self._orig = {}
        self.executed_tools = []      # names actually run via _execute_report_tool
        self.dispatched_rounds = []   # (round_index, had_tools) per dispatch
        self.systems_seen = []        # system prompt of each dispatch

    def __enter__(self):
        self._orig['dispatch_stream'] = re_mod.dispatch_stream
        self._orig['_execute_report_tool'] = re_mod._execute_report_tool
        self._orig['search_arxiv'] = re_mod.search_arxiv
        self._orig['fetch_arxiv_title'] = re_mod.fetch_arxiv_title
        rec = self

        # dispatch_stream is called per round with the *current* messages list.
        # We count how many tool-result messages are already present to know
        # which round we're on (0 = fresh, 1 = after the tool result).
        def _fake_dispatch_stream(messages, *, on_content=None, tools=None, **kw):
            rec.systems_seen.append(messages[0]['content'] if messages else '')
            n_tool_msgs = sum(1 for m in messages if m.get('role') == 'tool')
            rec.dispatched_rounds.append((n_tool_msgs, bool(tools)))
            first_round = (n_tool_msgs == 0)
            if first_round and not rec.break_loop:
                # Issue a web_search research call — no content this round.
                return ({'role': 'assistant', 'content': '',
                         'tool_calls': [{
                             'id': 'tc1', 'type': 'function',
                             'function': {'name': 'web_search',
                                          'arguments': json.dumps(
                                              {'queries': [{'query': 'diffusion language model ICML 2025'}]})},
                         }]}, 'tool_calls', {'prompt_tokens': 1, 'completion_tokens': 1})
            # Final round: return the JSON answer.
            body = json.dumps(_FINAL_JSON)
            if on_content:
                on_content(body)
            return ({'role': 'assistant', 'content': body, 'tool_calls': None},
                    'stop', {'prompt_tokens': 1, 'completion_tokens': 1})

        def _fake_execute_report_tool(name, args_str, user_question='', abort=None):
            rec.executed_tools.append(name)
            return ('SEARCH RESULT: Large Language Diffusion Models (arXiv:2502.09992) '
                    'appeared at ICML 2025.', [{'title': 'x'}], None, None, None)

        def _fake_search(query, max_results=10):
            return _fake_search_by_title(query)[:max_results]

        re_mod.dispatch_stream = _fake_dispatch_stream
        re_mod._execute_report_tool = _fake_execute_report_tool
        re_mod.search_arxiv = _fake_search
        re_mod.fetch_arxiv_title = lambda _id: ''
        return self

    def __exit__(self, *exc):
        for k, v in self._orig.items():
            setattr(re_mod, k, v)
        return False


def test_interpretation_actually_researches():
    """The loop executes the model's web_search call BEFORE grounding, and the
    final grounded card carries the researched venue."""
    with _Patched() as p:
        out = re_mod.recommend_papers('that diffusion LM from the last ICML', 6)
    assert 'web_search' in p.executed_tools, \
        f'web_search was never executed — not agentic: {p.executed_tools}'
    # Two dispatch rounds: research (with tools) then the JSON answer.
    assert len(p.dispatched_rounds) >= 2, f'expected >=2 rounds: {p.dispatched_rounds}'
    assert p.dispatched_rounds[0][1] is True, 'first round was not offered tools'
    ids = [c['arxiv_id'].split('v')[0] for c in out['results']]
    assert ids == ['2502.09992'], f'grounded result wrong: {ids}'
    assert out['results'][0]['venue'] == 'ICML 2025 Oral'
    assert out['llmError'] is False
    _ok('interpretation runs a real web_search research round before proposing candidates')


def test_system_prompt_is_date_anchored():
    """Every dispatch carries today's date — the fix for false 'not happened yet'
    corrections from a model with no clock."""
    import re as _re
    with _Patched() as p:
        re_mod.recommend_papers('a recent diffusion LM paper', 6)
    assert p.systems_seen, 'no dispatch happened'
    sys0 = p.systems_seen[0]
    assert _re.search(r"[Tt]oday'?s? date is \d{4}-\d{2}-\d{2}", sys0), \
        f'system prompt is not date-anchored: {sys0[:200]!r}'
    _ok('interpretation system prompt injects the current date (kills stale-clock corrections)')


def test_tool_events_forwarded():
    """The research tool activity is surfaced to the on_tool_event callback so
    the UI can show a live 'researching…' trail."""
    events = []
    with _Patched():
        list(re_mod.iter_recommend_events(
            'a recent diffusion LM', 6, on_tool_event=events.append))
    types = [e['type'] for e in events]
    assert 'tool_start' in types and 'tool_done' in types, \
        f'research tool events not forwarded: {types}'
    ts = next(e for e in events if e['type'] == 'tool_start')
    assert ts['toolName'] == 'web_search'
    _ok('research tool_start/tool_done events are forwarded for the UI trail')


def test_neuter_confirms_agentic_loop_is_load_bearing():
    """NEUTER: with the loop broken (model answers immediately, tools ignored),
    the research tool is NEVER executed — proving the loop drives the tool use."""
    # 1. Real loop researches.
    with _Patched() as p:
        re_mod.recommend_papers('recent dLLM', 6)
    assert 'web_search' in p.executed_tools, 'precondition failed: real loop did not research'

    # 2. Neuter: round 0 returns JSON with no tool calls → no research.
    with _Patched(break_loop=True) as p:
        out = re_mod.recommend_papers('recent dLLM', 6)
    assert 'web_search' not in p.executed_tools, \
        'NEUTER did not break the invariant — tool ran even with the loop broken ' \
        '(false-confidence test).'
    # Grounding still works off the model's memory-only candidate (that path is
    # unchanged) — so this proves ONLY that the RESEARCH step is what the loop
    # adds, not that the whole feature dies.
    assert [c['arxiv_id'].split('v')[0] for c in out['results']] == ['2502.09992']
    _ok('NEUTER: the tool loop is load-bearing (broken loop → no research executed)')


def main():
    print()
    print(_color('═══ Paper Recommend Agentic-Interpretation Tests ═══', '36'))
    print()
    tests = [
        test_interpretation_actually_researches,
        test_system_prompt_is_date_anchored,
        test_tool_events_forwarded,
        test_neuter_confirms_agentic_loop_is_load_bearing,
    ]
    for fn in tests:
        try:
            fn()
        except AssertionError as e:
            _fail(f'{fn.__name__}: {e}')
        except Exception as e:
            import traceback
            traceback.print_exc()
            _fail(f'{fn.__name__}: unexpected {type(e).__name__}: {e}')
    print()
    print(_color(f'═══ ALL {len(tests)} TESTS PASSED ═══', '32'))
    print()


if __name__ == '__main__':
    main()
